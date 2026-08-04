"""Binding-less lease concern adapter and containment scan (P0-1).

Kept out of ``execution_worker_reconciler.py`` so that file stays under the
400-line policy, mirroring the ``process_lease_payload`` split. This module owns
the binding-less lease concern adapter, the outcome recorder, and the
bidirectional active-lease containment scan -- nothing here holds policy beyond
what a single proof or a single recorded outcome means.
"""

from __future__ import annotations

from ...domain.execution_worker import TERMINAL_STATES as TERMINAL_BINDING_STATES
from ...domain.execution_worker import ExecutionWorkerBinding
from ...domain.process_lease import (
    ACTIVE_LEASE_STATUSES,
    ProcessLease,
    ProcessLeaseRole,
    ProcessLeaseStatus,
)
from ...ports.process_lease_store import ProcessLeaseStore
from .execution_worker_concerns import (
    ACTIVE_BINDING_STATES,
    FENCE_LEASE_STATUSES,
    TERMINAL_OUTCOMES,
    WorkerConcern,
    lease_registry_digest,
    registry_digest,
)
from .execution_worker_reclaim_report import ExecutionWorkerReclamationReport
from .worker_lifecycle import WorkerLifecycleStore


class LeaseConcern:
    """A binding-less active ProcessLease shaped like a worker concern (P0-1).

    The canonical ProcessLease is the safety authority; the binding is a derived
    projection that can be lost (crash between the lease write and the binding
    write, or a binding-store failure). A RUNNING lease whose binding is gone is
    still a real worker that may hold locks, so the reconciler must be able to
    prove and reclaim it from the lease alone -- the same fields a binding
    carries (pid, pgid, start token, owner identity, release sha) live on the
    lease, so every proof and reap helper works unchanged.
    """

    __slots__ = ("lease",)

    def __init__(self, lease: ProcessLease) -> None:
        self.lease = lease

    @property
    def worker_id(self) -> str:
        return self.lease.lease_id

    @property
    def pid(self) -> int:
        return self.lease.pid if self.lease.pid is not None else 0

    @property
    def pgid(self) -> int:
        return self.lease.pgid if self.lease.pgid is not None else 0

    @property
    def process_start_token(self) -> str | None:
        return self.lease.process_start_token

    @property
    def supervisor_pid(self) -> int:
        return self.lease.owner_pid if self.lease.owner_pid is not None else 0

    @property
    def supervisor_process_identity(self) -> str | None:
        return self.lease.owner_process_identity

    @property
    def release_sha(self) -> str | None:
        return self.lease.release_sha

    @property
    def child_pid(self) -> int:
        return self.pid

    @property
    def child_pgid(self) -> int:
        return self.pgid

    @property
    def child_start_token(self) -> str | None:
        return self.lease.process_start_token


def record_outcome(
    lifecycle: WorkerLifecycleStore,
    concern: WorkerConcern,
    state: str,
    counts: dict[str, int],
    worker_ids: list[str],
    pids: list[int],
    release_shas: list[str],
    persistence_failure_ids: list[str],
) -> None:
    """Persist one reaping outcome through the shared lifecycle service.

    A terminal outcome (``reclaimed``/``already_gone``) is only counted when it
    durably landed on the canonical lease (the safety authority, P0-1); a missing
    binding projection never fails it. Keep-active outcomes
    (``survived_kill``/``refused_unproven``) are counted regardless -- the process
    reality blocks -- but a persistence failure is recorded so the report never
    claims the lifecycle changed.
    """
    worker_id = concern.worker_id
    result = lifecycle.apply_outcome(worker_id, state)
    if state in TERMINAL_OUTCOMES:
        if not result.persisted:
            persistence_failure_ids.append(worker_id)
            return
        counts[state] += 1
        worker_ids.append(worker_id)
        pids.append(concern.pid)
        if concern.release_sha is not None:
            release_shas.append(concern.release_sha)
        return
    counts[state] += 1
    if not result.persisted:
        persistence_failure_ids.append(worker_id)


def scan_process_leases(
    leases: ProcessLeaseStore | None,
    bindings: tuple[ExecutionWorkerBinding, ...],
) -> tuple[int, int, bool, tuple[str, ...], int]:
    """Bidirectional active lease <-> binding containment scan.

    REGISTERED/READY leases are the durable spawn fence (P1-3) and count as
    ``incomplete``. Every other active lease and every active binding must name
    the same worker with the same pid and start token; a mismatch in either
    direction (lease without binding, binding without lease, a terminal binding
    alongside an ACTIVE lease, or a state split-brain) is a divergence.

    Terminal binding leftovers whose lease is gone are NOT a live-process
    divergence: a binding terminalized by a crash before its archive describes
    no process that may hold locks, so it counts as ``terminal_binding_debt``
    (repairable maintenance) instead of blocking a preflight forever.

    A RUNNING lease (the modern proven pair) without a pid or start token cannot
    prove the process it names and counts as ``incomplete`` -- fail closed on
    identity evidence, never assume.

    Terminal history never participates: ``list_active_page`` excludes it from
    both the records and the completeness signal, so 2,001 archived leases can
    never make a read-only preflight fail closed forever.
    """
    if leases is None:
        return 0, 0, True, (), 0
    scan = getattr(leases, "list_active_page", None)
    page = (
        scan(role=ProcessLeaseRole.EXECUTION_DAEMON)
        if scan is not None
        else leases.list_page(role=ProcessLeaseRole.EXECUTION_DAEMON)
    )
    # Terminal history never participates: the active-only listing excludes it
    # in production, and this filter makes the fallback path behave identically
    # so a lease terminalized by this pass cannot be misread as a divergence.
    by_lease = {
        lease.lease_id: lease for lease in page.records if lease.status in ACTIVE_LEASE_STATUSES
    }
    by_binding = {binding.worker_id: binding for binding in bindings}
    incomplete = 0
    divergence = 0
    terminal_binding_debt = 0
    for worker_id in sorted(set(by_lease) | set(by_binding)):
        lease = by_lease.get(worker_id)
        binding = by_binding.get(worker_id)
        if lease is not None and lease.status in FENCE_LEASE_STATUSES:
            # An in-flight spawn (pre-spawn intent or unclaimed pid) is the
            # durable fence member (P1-3): it may claim READY after the
            # incumbent is stopped, so it blocks the final preflight.
            incomplete += 1
            continue
        if lease is None and binding is None:
            continue
        if lease is None:
            if binding is None:
                continue
            if binding.state in TERMINAL_BINDING_STATES:
                # A terminal binding left in the active store (archive debt):
                # it describes no process that may hold locks, so it is
                # repairable maintenance, never a live-process divergence.
                terminal_binding_debt += 1
                continue
            # An active binding with no active lease -- the lease is missing
            # entirely or already terminal. Either way the two registries
            # disagree about a worker that may still be running.
            divergence += 1
            continue
        if binding is None:
            # An active lease no binding names: an orphan a later pass cannot
            # reclaim through the projection.
            divergence += 1
            continue
        if binding.state in TERMINAL_BINDING_STATES:
            # The projection says the worker is terminal but the canonical lease
            # is still active: a real split-brain (the process may hold locks).
            divergence += 1
            continue
        if binding.state not in ACTIVE_BINDING_STATES:
            divergence += 1
            continue
        if (
            binding.state == "running"
            and lease.status is ProcessLeaseStatus.RUNNING
            and (lease.pid is None or lease.process_start_token is None)
        ):
            # A modern proven pair without the identity proof that makes the
            # pair provable: incomplete evidence, fail closed.
            incomplete += 1
            continue
        if lease.pid is not None and binding.pid is not None and lease.pid != binding.pid:
            divergence += 1
            continue
        if (
            lease.process_start_token is not None
            and binding.process_start_token is not None
            and lease.process_start_token != binding.process_start_token
        ):
            divergence += 1
            continue
        if state_matrix_divergent(binding.state, lease.status):
            divergence += 1
    return incomplete, divergence, page.scan_complete, page.unreadable_ids, terminal_binding_debt


def state_matrix_divergent(binding_state: str, lease_status: ProcessLeaseStatus) -> bool:
    """A binding/lease state pair that cannot both describe the same worker.

    The allowed pairs are exact (review F-010): each binding state admits only
    the lease states that describe the same durable reality --

    * ``running``       <-> RUNNING (or the transient TERMINATING the lifecycle
                         writes mid-outcome);
    * ``legacy_unproven`` <-> a legacy imported RUNNING lease;
    * ``refused_unproven`` <-> UNPROVEN;
    * ``survived_kill``  <-> KILLED.

    Anything else -- e.g. ``refused_unproven`` with a KILLED lease or
    ``survived_kill`` with an UNPROVEN one -- claims two different histories for
    one worker and is a split-brain.
    """
    if binding_state == "running":
        return lease_status not in {
            ProcessLeaseStatus.RUNNING,
            ProcessLeaseStatus.TERMINATING,
        }
    if binding_state == "legacy_unproven":
        return lease_status is not ProcessLeaseStatus.RUNNING
    if binding_state == "refused_unproven":
        return lease_status is not ProcessLeaseStatus.UNPROVEN
    if binding_state == "survived_kill":
        return lease_status is not ProcessLeaseStatus.KILLED
    return True


def digest(
    bindings: tuple[ExecutionWorkerBinding, ...], *, leases: ProcessLeaseStore | None
) -> str:
    """One digest over whichever live-concern source is authoritative.

    When leases are wired (production), the digest covers the lease set; the
    binding digest remains for the binding-only test path.
    """
    if leases is None:
        return registry_digest(bindings)
    scan = getattr(leases, "list_active_page", None)
    page = (
        scan(role=ProcessLeaseRole.EXECUTION_DAEMON)
        if scan is not None
        else leases.list_page(role=ProcessLeaseRole.EXECUTION_DAEMON)
    )
    return lease_registry_digest(page.records)


__all__ = [
    "ExecutionWorkerReclamationReport",
    "LeaseConcern",
    "digest",
    "record_outcome",
    "scan_process_leases",
    "state_matrix_divergent",
]
