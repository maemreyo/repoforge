"""Parity checking between authoritative JSON state and the SQLite shadow.

JSON is authoritative; the SQLite lease table is a shadow replica for fast
diffing. The parity check compares the logical snapshot of each side and reports
which lease ids exist on only one side -- drift, not a verdict: no safety gate
reads SQLite, so a divergence is evidence for the operator, never a state change.
"""

from __future__ import annotations

from dataclasses import dataclass

from ...domain.execution_worker import ExecutionWorkerBinding
from ...domain.process_lease import ProcessLeaseRole
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
    json_scan_complete: bool
    json_unreadable_ids: tuple[str, ...]

    @property
    def in_sync(self) -> bool:
        return not (self.only_in_json or self.only_in_shadow) and self.json_scan_complete

    def as_dict(self) -> dict[str, object]:
        return {
            "json_count": self.json_count,
            "shadow_count": self.shadow_count,
            "only_in_json": list(self.only_in_json),
            "only_in_shadow": list(self.only_in_shadow),
            "json_scan_complete": self.json_scan_complete,
            "json_unreadable_ids": list(self.json_unreadable_ids),
            "in_sync": self.in_sync,
        }


def compare_lease_parity(
    leases: JsonProcessLeaseAdapter,
    shadow: SqliteLeaseStore,
) -> ParityReport:
    """Compare authoritative JSON leases against shadow leases.

    The shadow mirrors every authoritative lease write, so the two should agree.
    A divergence means a write reached only one side -- evidence for the operator
    that the mirror or the authoritative store lagged behind.
    """
    page = leases.list_page()
    json_ids = {lease.lease_id for lease in page.records}
    shadow_ids = {lease.lease_id for lease, _ in shadow.list_all()}
    return ParityReport(
        json_count=len(json_ids),
        shadow_count=len(shadow_ids),
        only_in_json=tuple(sorted(json_ids - shadow_ids)),
        only_in_shadow=tuple(sorted(shadow_ids - json_ids)),
        json_scan_complete=page.scan_complete,
        json_unreadable_ids=page.unreadable_ids,
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
    from ...domain.process_lease import ProcessLease, ProcessLeaseStatus

    imported = 0
    for binding in _active_bindings(bindings):
        lease = ProcessLease(
            lease_id=binding.worker_id,
            role=ProcessLeaseRole.EXECUTION_DAEMON,
            status=ProcessLeaseStatus.RUNNING,
            process_identity=binding.process_start_token,
            pid=binding.pid,
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
