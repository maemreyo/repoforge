"""Parity checking between authoritative JSON state and the SQLite shadow.

JSON is authoritative; the SQLite lease table is a shadow replica for fast
diffing. The parity check compares the logical snapshot of each side -- identity
sets *and* per-lease content/revision -- and reports drift. No safety gate reads
SQLite, so a divergence is evidence for the operator, never a state change.
"""

from __future__ import annotations

from dataclasses import dataclass

from ...domain.durable_state import Revision
from ...domain.execution_worker import ExecutionWorkerBinding
from ...domain.process_lease import ProcessLease, ProcessLeaseRole
from ...ports.execution_worker_store import ExecutionWorkerBindingStore
from .json_process_lease_adapter import JsonProcessLeaseAdapter
from .sqlite_lease_store import SqliteLeaseStore


@dataclass(frozen=True, slots=True)
class ParityReport:
    """One logical-snapshot diff of the lease registry, bounded and read-only."""

    json_count: int
    shadow_count: int
    only_in_json: tuple[str, ...]
    only_in_shadow: tuple[str, ...]
    content_mismatch: tuple[str, ...]
    revision_mismatch: tuple[str, ...]
    json_scan_complete: bool
    json_unreadable_ids: tuple[str, ...]
    shadow_scan_complete: bool
    shadow_unreadable_ids: tuple[str, ...]

    @property
    def in_sync(self) -> bool:
        return (
            not (
                self.only_in_json
                or self.only_in_shadow
                or self.content_mismatch
                or self.revision_mismatch
            )
            and self.json_scan_complete
            and not self.json_unreadable_ids
            and self.shadow_scan_complete
            and not self.shadow_unreadable_ids
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "json_count": self.json_count,
            "shadow_count": self.shadow_count,
            "only_in_json": list(self.only_in_json),
            "only_in_shadow": list(self.only_in_shadow),
            "content_mismatch": list(self.content_mismatch),
            "revision_mismatch": list(self.revision_mismatch),
            "json_scan_complete": self.json_scan_complete,
            "json_unreadable_ids": list(self.json_unreadable_ids),
            "shadow_scan_complete": self.shadow_scan_complete,
            "shadow_unreadable_ids": list(self.shadow_unreadable_ids),
            "in_sync": self.in_sync,
        }


def _lease_content_digest(lease: ProcessLease) -> str:
    """Canonical logical content of one lease, excluding revision and timestamps.

    Timestamps (created_at/updated_at/started_at/heartbeat_at) can legitimately
    differ across mirror lag without meaning a safety divergence; the safety-
    relevant identity is status, role, pid, process_identity, correlation, and
    error disposition.
    """
    return "|".join(
        (
            lease.lease_id,
            lease.status.value,
            lease.role.value,
            "" if lease.pid is None else str(lease.pid),
            lease.process_identity or "",
            lease.correlation_id,
            lease.error_code or "",
            lease.error_message or "",
        )
    )


def compare_lease_parity(
    leases: JsonProcessLeaseAdapter,
    shadow: SqliteLeaseStore,
    *,
    max_records: int = 2_000,
) -> ParityReport:
    """Compare authoritative JSON leases against shadow leases.

    The shadow mirrors every authoritative lease write, so the two should agree
    on both membership and logical content. A divergence means a write reached
    only one side, or a final transition failed on the shadow -- evidence for
    the operator that the mirror lagged behind.

    Both sides are scanned with the same ``max_records`` bound, and ``in_sync``
    fails closed unless *each* side reports a complete, readable scan: a shadow
    row beyond the bound or a malformed shadow row is invisible to the diff, so
    either must keep the report out of sync (F-008).
    """
    page = leases.list_page(max_records=max_records)
    json_by_id: dict[str, tuple[ProcessLease, Revision]] = {}
    for lease in page.records:
        envelope = leases.read(lease.lease_id)
        if envelope is None:
            continue
        json_by_id[lease.lease_id] = (envelope.value, envelope.revision)

    shadow_page = shadow.list_envelopes_page(max_records=max_records)
    shadow_by_id: dict[str, tuple[ProcessLease, Revision]] = {
        lease.lease_id: (lease, revision) for lease, revision in shadow_page.records
    }

    json_ids = set(json_by_id)
    shadow_ids = set(shadow_by_id)
    shared = json_ids & shadow_ids

    content_mismatch: list[str] = []
    revision_mismatch: list[str] = []
    for lease_id in sorted(shared):
        json_lease, json_revision = json_by_id[lease_id]
        shadow_lease, shadow_revision = shadow_by_id[lease_id]
        if _lease_content_digest(json_lease) != _lease_content_digest(shadow_lease):
            content_mismatch.append(lease_id)
        if json_revision != shadow_revision:
            revision_mismatch.append(lease_id)

    return ParityReport(
        json_count=len(json_ids),
        shadow_count=len(shadow_ids),
        only_in_json=tuple(sorted(json_ids - shadow_ids)),
        only_in_shadow=tuple(sorted(shadow_ids - json_ids)),
        content_mismatch=tuple(content_mismatch),
        revision_mismatch=tuple(revision_mismatch),
        json_scan_complete=page.scan_complete,
        json_unreadable_ids=page.unreadable_ids,
        shadow_scan_complete=shadow_page.scan_complete,
        shadow_unreadable_ids=shadow_page.unreadable_ids,
    )


def import_active_bindings(
    bindings: ExecutionWorkerBindingStore,
    leases: JsonProcessLeaseAdapter,
    shadow: SqliteLeaseStore,
    *,
    now: str,
) -> int:
    """Migrate existing active execution-worker bindings into the lease registry.

    Phase 3 migration step: active bindings written before the pre-spawn lease
    protocol are imported as RUNNING leases into the authoritative JSON lease
    store (and mirrored to the shadow), so parity starts from a populated lease
    registry instead of an empty one. Returns the number of leases written.
    """
    from ...domain.process_lease import ProcessLeaseStatus

    #: Imported leases must keep the SAME durable reality as the binding they
    #: came from, or the containment matrix (F-010) reads every import as a
    #: split-brain: a refused_unproven binding with a RUNNING lease diverges, as
    #: does a survived_kill binding with a RUNNING lease.
    _STATUS_BY_BINDING_STATE = {
        "running": ProcessLeaseStatus.RUNNING,
        "legacy_unproven": ProcessLeaseStatus.RUNNING,
        "refused_unproven": ProcessLeaseStatus.UNPROVEN,
        "survived_kill": ProcessLeaseStatus.KILLED,
    }

    imported = 0
    for binding in _active_bindings(bindings):
        lease = ProcessLease(
            lease_id=binding.worker_id,
            role=ProcessLeaseRole.EXECUTION_DAEMON,
            status=_STATUS_BY_BINDING_STATE[binding.state],
            process_identity=binding.process_start_token,
            pid=binding.pid,
            pgid=binding.pgid,
            process_start_token=binding.process_start_token,
            owner_pid=binding.supervisor_pid,
            owner_process_identity=binding.supervisor_process_identity,
            release_sha=binding.release_sha,
            generation=binding.generation,
            started_at=binding.started_at,
            heartbeat_at=binding.started_at,
            correlation_id=binding.correlation_id,
            created_at=binding.started_at,
            updated_at=now,
        )
        envelope = leases.create(lease)
        shadow.write_shadow(lease, envelope.revision)
        imported += 1
    return imported


def _active_bindings(
    bindings: ExecutionWorkerBindingStore,
) -> tuple[ExecutionWorkerBinding, ...]:
    page = bindings.list_page()
    active_states = frozenset({"running", "legacy_unproven", "refused_unproven", "survived_kill"})
    return tuple(binding for binding in page.records if binding.state in active_states)


__all__ = [
    "ParityReport",
    "compare_lease_parity",
    "import_active_bindings",
]
