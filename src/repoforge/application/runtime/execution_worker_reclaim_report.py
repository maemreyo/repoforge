"""Reclamation report model for execution-worker reconciliation (#368).

Kept out of ``execution_worker_reconciler.py`` so that file stays under the
400-line policy, mirroring the ``process_lease_payload`` split. This is the pure
evidence snapshot a reconciliation pass returns, for activation receipts and
doctor output.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ExecutionWorkerReclamationReport:
    """Bounded evidence of one reconciliation pass, for receipts and doctor output."""

    inspected: int
    reclaimed: int
    already_gone: int
    refused_unproven: int
    survived_kill: int
    possibly_alive_unproven: int
    scan_complete: bool
    unreadable_record_ids: tuple[str, ...]
    worker_ids: tuple[str, ...]
    pids: tuple[int, ...]
    release_shas: tuple[str, ...]
    detail: str
    #: Fence token (F-004): a digest of the live-concern lease set seen by this pass.
    registry_digest: str = ""
    process_lease_incomplete: int = 0
    process_lease_binding_divergence: int = 0
    process_lease_scan_complete: bool = True
    process_lease_unreadable_ids: tuple[str, ...] = ()
    #: Lifecycle outcomes the pass could NOT persist durably (binding or lease
    #: CAS failed). The durable registry still shows the worker active, so the
    #: pass must never claim it reclaimed/already-gone; it is a fail-closed gate.
    persistence_failures: int = 0
    persistence_failure_ids: tuple[str, ...] = ()
    #: Terminal-record collections (binding/lease/shadow sweep) that RAISED. GC is
    #: best-effort, but a silent failure lets the active scan grow without bound,
    #: so it is reported as evidence, never hidden.
    maintenance_failures: int = 0
    maintenance_failure_ids: tuple[str, ...] = ()
    #: Terminal bindings still in the active store after the maintenance sweep
    #: (archive debt). Repairable maintenance, never a live-process divergence:
    #: a terminal binding describes no process that may hold locks.
    terminal_binding_debt: int = 0

    @property
    def evidence_complete(self) -> bool:
        """Fail-closed evidence: an unreadable record hides an orphan like a truncation."""
        return (
            self.scan_complete
            and not self.unreadable_record_ids
            and self.process_lease_scan_complete
            and not self.process_lease_unreadable_ids
        )

    @property
    def blocker_code(self) -> str | None:
        """One canonical code per gate so every caller reports the same blocker (#424)."""
        if self.survived_kill > 0:
            return "STALE_EXECUTION_WORKER_RECLAMATION_FAILED"
        if self.possibly_alive_unproven > 0:
            return "STALE_EXECUTION_WORKER_IDENTITY_UNPROVEN"
        if self.process_lease_incomplete > 0:
            return "PROCESS_LEASE_INCOMPLETE"
        if self.process_lease_binding_divergence > 0:
            return "PROCESS_LEASE_BINDING_DIVERGENCE"
        if self.persistence_failures > 0:
            return "PROCESS_LEASE_PERSISTENCE_FAILURE"
        if not self.evidence_complete:
            if self.unreadable_record_ids or self.process_lease_unreadable_ids:
                return "EXECUTION_WORKER_REGISTRY_UNREADABLE_RECORDS"
            return "EXECUTION_WORKER_REGISTRY_SCAN_INCOMPLETE"
        return None

    def as_dict(self) -> dict[str, object]:
        return {
            "inspected": self.inspected,
            "reclaimed": self.reclaimed,
            "already_gone": self.already_gone,
            "refused_unproven": self.refused_unproven,
            "survived_kill": self.survived_kill,
            "possibly_alive_unproven": self.possibly_alive_unproven,
            "scan_complete": self.scan_complete,
            "unreadable_record_ids": list(self.unreadable_record_ids),
            "evidence_complete": self.evidence_complete,
            "blocker_code": self.blocker_code,
            "registry_digest": self.registry_digest,
            "worker_ids": list(self.worker_ids),
            "pids": list(self.pids),
            "release_shas": list(self.release_shas),
            "detail": self.detail,
            "process_lease_incomplete": self.process_lease_incomplete,
            "process_lease_binding_divergence": self.process_lease_binding_divergence,
            "process_lease_scan_complete": self.process_lease_scan_complete,
            "process_lease_unreadable_ids": list(self.process_lease_unreadable_ids),
            "persistence_failures": self.persistence_failures,
            "persistence_failure_ids": list(self.persistence_failure_ids),
            "maintenance_failures": self.maintenance_failures,
            "maintenance_failure_ids": list(self.maintenance_failure_ids),
            "terminal_binding_debt": self.terminal_binding_debt,
        }


__all__ = ["ExecutionWorkerReclamationReport"]
