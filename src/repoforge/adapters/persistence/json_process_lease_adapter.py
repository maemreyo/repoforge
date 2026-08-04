"""Authoritative JSON persistence for ProcessLeaseStore.

The pre-spawn worker intent (F-001) must be durably recorded BEFORE a process is
spawned so a crash can never leave an invisible orphan: this store is the
authoritative home for that intent and for the lease lifecycle that follows it.
The SQLite shadow mirrors these writes for parity checking.

Active/terminal separation: terminal leases (TERMINATED/ARCHIVED) are archived
into a sibling ``runtime-process-leases-history`` collection by
``collect_terminal``, and the active scan (``list_active_page``) never counts them
toward completeness -- so a read-only preflight cannot be permanently blocked by
accumulated terminal history.
"""

from __future__ import annotations

import re
from pathlib import Path

from ...domain.durable_state import Revision, SchemaVersion, StateCodec, StateEnvelope, StatePage
from ...domain.process_lease import (
    ACTIVE_LEASE_STATUSES,
    ProcessLease,
    ProcessLeaseRole,
    ProcessLeaseStatus,
    process_lease_from_payload,
    process_lease_payload,
)
from ...ports.locking import LockManager
from ...ports.process_lease_store import ProcessLeasePage
from .json_state_repository import JsonStateRepository

PROCESS_LEASE_SCHEMA_VERSION = 1

_LEASE_ID = re.compile(r"^[a-z0-9]+-[a-f0-9]{6,64}$")

#: Hard cap on how many raw records the full listing may enumerate before failing
#: closed on completeness. Terminal history is excluded from the active-scan
#: completeness computation (the active scan pages instead), so this bound is a
#: protection against an unbounded active dir, not a way for terminal history to
#: hide an active orphan.
_ACTIVE_SCAN_CAP = 100_000

#: Page size for the paged active scan: bounded decode work per enumeration
#: round, while terminal records are skipped without counting against
#: completeness (F-008).
_SCAN_PAGE_SIZE = 2_000

_TERMINAL_STATUSES = frozenset({ProcessLeaseStatus.TERMINATED, ProcessLeaseStatus.ARCHIVED})


def validate_lease_id(lease_id: str) -> str:
    if not isinstance(lease_id, str) or _LEASE_ID.fullmatch(lease_id) is None:
        raise ValueError(
            "process lease id must look like <kind>-<hex> (e.g. worker-0000... or lease-0000...)"
        )
    return lease_id


class _ProcessLeaseCodec(StateCodec[ProcessLease]):
    schema_version = SchemaVersion(PROCESS_LEASE_SCHEMA_VERSION)

    def encode(self, value: ProcessLease) -> dict[str, object]:
        return process_lease_payload(value)

    def decode(self, payload: dict[str, object]) -> ProcessLease:
        return process_lease_from_payload(dict(payload))


class JsonProcessLeaseAdapter:
    """Authoritative JSON-backed ProcessLeaseStore."""

    def __init__(self, state_root: Path, locks: LockManager) -> None:
        self._records: JsonStateRepository[ProcessLease] = JsonStateRepository(
            state_root,
            collection="runtime-process-leases",
            locks=locks,
            codec=_ProcessLeaseCodec(),
            id_validator=validate_lease_id,
            max_record_bytes=8_192,
        )
        self._history: JsonStateRepository[ProcessLease] = JsonStateRepository(
            state_root,
            collection="runtime-process-leases-history",
            locks=locks,
            codec=_ProcessLeaseCodec(),
            id_validator=validate_lease_id,
            max_record_bytes=8_192,
        )
        self.root = self._records.root

    def create(self, lease: ProcessLease) -> StateEnvelope[ProcessLease]:
        validate_lease_id(lease.lease_id)
        return self._records.create(lease.lease_id, lease)

    def read(self, lease_id: str) -> StateEnvelope[ProcessLease] | None:
        validate_lease_id(lease_id)
        envelope = self._records.read(lease_id)
        return envelope if envelope is not None else self._history.read(lease_id)

    def save(
        self,
        lease: ProcessLease,
        *,
        expected_revision: Revision,
    ) -> StateEnvelope[ProcessLease]:
        validate_lease_id(lease.lease_id)
        return self._records.save(
            lease.lease_id,
            lease,
            expected_revision=expected_revision,
        )

    def list_all(self, *, max_records: int = 2_000) -> StatePage[ProcessLease]:
        return self._records.list_records(max_records=max_records)

    def delete(self, lease_id: str, *, expected_revision: Revision) -> bool:
        """Delete only while the stored revision still matches ``expected_revision``.

        Atomic under the per-record lock via ``JsonStateRepository.delete_if_revision``
        so a concurrent save between compare and delete cannot cause a stale writer
        to remove a lease that has since moved on (#424 follow-up).
        """
        validate_lease_id(lease_id)
        return self._records.delete_if_revision(lease_id, expected_revision=expected_revision)

    def list_page(
        self,
        *,
        role: ProcessLeaseRole | None = None,
        max_records: int = 2_000,
    ) -> ProcessLeasePage:
        return self._list(role=role, max_records=max_records, active_only=False)

    def list_active_page(
        self,
        *,
        role: ProcessLeaseRole | None = None,
        max_records: int = 2_000,
    ) -> ProcessLeasePage:
        return self._list(role=role, max_records=max_records, active_only=True)

    def collect_terminal(self, *, max_records: int = 5_000) -> int:
        """Archive terminal leases into the immutable history collection.

        A terminal lease is moved only under its per-record lock (read, then
        history-create, then active-delete); a concurrent save between read and
        delete leaves it in active for the next pass. Best-effort by design: this
        is maintenance, and a failure to archive must never fail a reconciliation
        that already proved the lease terminal.
        """
        moved = 0
        record_ids, _ = self._records.list_record_ids(max_records=max_records)
        for record_id in record_ids:
            envelope = self._records.read(record_id)
            if envelope is None or envelope.value.status not in _TERMINAL_STATUSES:
                continue
            try:
                self._history.create_or_read_equal(record_id, envelope.value)
                self._records.delete_if_revision(record_id, expected_revision=envelope.revision)
                moved += 1
            except Exception:
                # A concurrent save won the race, or the history write failed: the
                # lease stays active and the next mutating pass retries. Never let
                # terminal archiving fail the caller.
                continue
        return moved

    def archive_terminal(self, lease_id: str, *, expected_revision: Revision) -> bool:
        """Archive+delete one terminal record, CAS-bound to ``expected_revision``.

        A live (non-terminal) lease is refused, so a stale caller can never archive
        a record that has since moved back to RUNNING. A revision mismatch leaves
        the record in place for the next ``collect_terminal`` pass.
        """
        validate_lease_id(lease_id)
        envelope = self._records.read(lease_id)
        if envelope is None:
            return False
        if envelope.revision != expected_revision:
            return False
        if envelope.value.status not in _TERMINAL_STATUSES:
            return False
        try:
            self._history.create_or_read_equal(lease_id, envelope.value)
        except Exception:
            return False
        return self._records.delete_if_revision(lease_id, expected_revision=expected_revision)

    def _list(
        self,
        *,
        role: ProcessLeaseRole | None,
        max_records: int,
        active_only: bool,
    ) -> ProcessLeasePage:
        if (
            not isinstance(max_records, int)
            or isinstance(max_records, bool)
            or max_records <= 0
            or max_records > 2_000
        ):
            raise ValueError("max_records must be between 1 and 2000")
        if not active_only:
            # Full listing: one bounded enumeration over the whole collection. An
            # enumeration truncated by the cap is a partial scan, never a complete
            # one (fail closed on completeness, F-008).
            record_ids, enumeration_truncated = self._records.list_record_ids(
                max_records=_ACTIVE_SCAN_CAP
            )
            records, unreadable = self._read_records(record_ids, role=role, active_only=False)
            scan_complete = not enumeration_truncated and len(records) <= max_records
            return ProcessLeasePage(
                records=tuple(sorted(records, key=lambda lease: lease.lease_id)[:max_records]),
                scan_complete=scan_complete,
                unreadable_ids=tuple(sorted(unreadable)),
            )
        # Active scan: page through the collection and skip terminal records
        # WITHOUT counting them against the enumeration cap. Terminal history is
        # archived by the mutating prune pass, so a read-only preflight must never
        # fail closed just because terminal backlog exceeds a fixed cap -- the
        # active scan's completeness is computed over the ACTIVE set only (F-008).
        records, unreadable, collection_exhausted = self._scan_active_ids(
            role=role, max_records=max_records
        )
        scan_complete = collection_exhausted and len(records) <= max_records
        return ProcessLeasePage(
            records=tuple(sorted(records, key=lambda lease: lease.lease_id)[:max_records]),
            scan_complete=scan_complete,
            unreadable_ids=tuple(sorted(unreadable)),
        )

    def _scan_active_ids(
        self, *, role: ProcessLeaseRole | None, max_records: int
    ) -> tuple[list[ProcessLease], list[str], bool]:
        """Enumerate the collection in bounded pages, counting only active leases.

        Returns ``(records, unreadable, collection_exhausted)``. The scan pages
        through record ids with an offset and keeps only active-status leases, so
        arbitrarily deep terminal history neither truncates the active result nor
        falsifies its completeness: the scan is complete exactly when it has read
        the whole collection. If the active set itself outgrows ``max_records``,
        the scan stops early and reports the collection as not exhausted (a
        genuinely truncated active page).
        """
        records: list[ProcessLease] = []
        unreadable: list[str] = []
        offset = 0
        while True:
            record_ids, enumeration_truncated = self._records.list_record_ids(
                max_records=_SCAN_PAGE_SIZE, offset=offset
            )
            if not record_ids:
                return records, unreadable, not enumeration_truncated
            for record_id in record_ids:
                try:
                    envelope = self._records.read(record_id)
                except Exception:
                    unreadable.append(record_id)
                    continue
                if envelope is None:
                    continue
                lease = envelope.value
                if lease.status not in ACTIVE_LEASE_STATUSES:
                    continue
                if role is not None and lease.role is not role:
                    continue
                records.append(lease)
                if len(records) > max_records:
                    # More active leases than the caller asked for: the page is
                    # genuinely truncated at max_records, so completeness is False.
                    return records, unreadable, False
            offset += len(record_ids)
            if not enumeration_truncated:
                return records, unreadable, True

    def _read_records(
        self,
        record_ids: tuple[str, ...],
        *,
        role: ProcessLeaseRole | None,
        active_only: bool,
    ) -> tuple[list[ProcessLease], list[str]]:
        """Decode one bounded batch of record ids into leases and unreadable ids."""
        records: list[ProcessLease] = []
        unreadable: list[str] = []
        for record_id in record_ids:
            try:
                envelope = self._records.read(record_id)
            except Exception:
                unreadable.append(record_id)
                continue
            if envelope is None:
                continue
            lease = envelope.value
            if active_only and lease.status not in ACTIVE_LEASE_STATUSES:
                continue
            if role is not None and lease.role is not role:
                continue
            records.append(lease)
        return records, unreadable
