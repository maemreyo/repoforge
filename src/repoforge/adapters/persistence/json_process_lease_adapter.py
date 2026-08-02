"""Authoritative JSON persistence for ProcessLeaseStore.

The pre-spawn worker intent (F-001) must be durably recorded BEFORE a process is
spawned so a crash can never leave an invisible orphan: this store is the
authoritative home for that intent and for the lease lifecycle that follows it.
The SQLite shadow mirrors these writes for parity checking.
"""

from __future__ import annotations

import re
from pathlib import Path

from ...domain.durable_state import Revision, SchemaVersion, StateCodec, StateEnvelope, StatePage
from ...domain.process_lease import (
    ProcessLease,
    ProcessLeaseRole,
    process_lease_from_payload,
    process_lease_payload,
)
from ...ports.locking import LockManager
from ...ports.process_lease_store import ProcessLeasePage
from .json_state_repository import JsonStateRepository

PROCESS_LEASE_SCHEMA_VERSION = 1

_LEASE_ID = re.compile(r"^[a-z0-9]+-[a-f0-9]{6,64}$")


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
        self.root = self._records.root

    def create(self, lease: ProcessLease) -> StateEnvelope[ProcessLease]:
        validate_lease_id(lease.lease_id)
        return self._records.create(lease.lease_id, lease)

    def read(self, lease_id: str) -> StateEnvelope[ProcessLease] | None:
        validate_lease_id(lease_id)
        return self._records.read(lease_id)

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
        """Delete only while the stored revision still matches ``expected_revision``."""
        validate_lease_id(lease_id)
        envelope = self._records.read(lease_id)
        if envelope is None:
            return False
        if envelope.revision != expected_revision:
            return False
        self._records.delete(lease_id)
        return True

    def list_page(
        self,
        *,
        role: ProcessLeaseRole | None = None,
        max_records: int = 2_000,
    ) -> ProcessLeasePage:
        page = self._records.list_records(max_records=max_records)
        records = tuple(
            item.value for item in page.records if role is None or item.value.role is role
        )
        return ProcessLeasePage(
            records=records,
            scan_complete=not page.scan_truncated,
            unreadable_ids=page.unreadable_record_ids,
        )
