"""PID-safe reconciliation of orphaned execution workers (#368).

An execution worker is spawned by a supervisor with ``start_new_session=True`` and
is tracked in the supervisor's durable ProcessLease. When the supervisor dies, the
worker is reparented and keeps running -- holding operation locks that block the
next release from converging (92 workers in the 2026-08-01 incident). This
reconciler reclaims them before a new supervisor starts, proving each worker's
identity first:

1. the authoritative ProcessLease is the live-concern source; the legacy binding
   projection is never a safety gate (single authority);
2. the owner supervisor must be dead, or the worker must belong to a departing
   release -- a worker owned by a live supervisor is never touched;
3. the lease must carry a process start token (PID-reuse proof) and the process
   must still be the same one;
4. its command line must be exactly the execution-worker entry point;
5. only then is the process group TERM->KILL reaped with a bounded wait.

Fail-closed policy (#420): an unclassifiable worker whose process AND group are
provably gone is ``already_gone`` (safe); one that may still be running is
``refused_unproven`` and counted as ``possibly_alive_unproven`` so the caller can
block starting a replacement -- not killing is not safety. Incomplete evidence is
likewise a block: an incomplete registry scan (``scan_complete=False``) or an
unreadable registry record (``unreadable_record_ids``) can hide an orphan that is
holding locks, so ``evidence_complete`` folds both into one fail-closed signal.

Admission fence (P1-3): REGISTERED/READY leases are the durable spawn fence --
while one exists the incumbent must not be stopped, because a spawn started
before the fence can claim READY after the incumbent is gone. They are reported
as ``process_lease_incomplete`` and block the final preflight.

The evidence model, the binding-less lease concern adapter, and the pure
identity/containment proofs live in the sibling ``execution_worker_reclaim``
module so this file stays under the 400-line policy; ``ExecutionWorkerReclamationReport``
is re-exported here so the canonical import path is unchanged.
"""

from __future__ import annotations

from collections.abc import Callable

from ...domain.execution_worker import ExecutionWorkerBinding
from ...domain.process_lease import (
    ProcessLeaseRole,
)
from ...ports.execution_worker_store import ExecutionWorkerBindingStore
from ...ports.process_lease_store import ProcessLeaseStore
from ...ports.process_reaper import ProcessReaper
from .execution_worker_concerns import (
    ACTIVE_BINDING_STATES,
    CommandLineReader,
    NowIso,
    OwnerIdentityReader,
    ProcessGroupGoneReader,
    ProcessIdentityReader,
    provably_gone,
    proven_execution_worker,
    still_owned,
)
from .execution_worker_reclaim import (
    LeaseConcern,
    digest,
    record_outcome,
    scan_process_leases,
)
from .execution_worker_reclaim_report import ExecutionWorkerReclamationReport
from .worker_lifecycle import WorkerLifecycleStore

__all__ = [
    "ExecutionWorkerReclamationReport",
    "ExecutionWorkerReconciler",
]


class ExecutionWorkerReconciler:
    def __init__(
        self,
        *,
        bindings: ExecutionWorkerBindingStore,
        reaper: ProcessReaper,
        owner_identity_reader: OwnerIdentityReader,
        command_line_reader: CommandLineReader,
        identity_reader: ProcessIdentityReader,
        process_group_gone: ProcessGroupGoneReader | None = None,
        leases: ProcessLeaseStore | None = None,
        now_iso: NowIso | None = None,
        lifecycle: WorkerLifecycleStore | None = None,
    ) -> None:
        self._bindings = bindings
        self._reaper = reaper
        self._owner_identity_reader = owner_identity_reader
        self._command_line_reader = command_line_reader
        self._identity_reader = identity_reader
        self._process_group_gone = process_group_gone
        self._leases = leases
        self._now_iso = now_iso
        self._lifecycle = (
            lifecycle
            if lifecycle is not None
            else WorkerLifecycleStore(
                bindings=bindings,
                leases=leases,
                shadow=None,
                now_iso=now_iso,
                # With no lease authority the reconciler persists only the binding
                # projection (binding-only opt-out); production always wires a
                # lease store, so there the lifecycle is strict (F-010).
                binding_only=leases is None,
            )
        )

    def reconcile(
        self,
        *,
        departing_releases: frozenset[str] = frozenset(),
        read_only: bool = False,
    ) -> ExecutionWorkerReclamationReport:
        """Evaluate and reclaim orphaned/departing workers, bounded and evidence-backed.

        ``read_only=True`` answers the same blocker questions without any side
        effect: no reaping, no state marks, no terminal-lease collection. A
        restarter uses it as a preflight before stopping a healthy runtime (#424).

        The canonical ProcessLease is the safety authority (P0-1): the binding is
        a derived projection that can be lost, so a binding-less active lease is a
        directly reclaimable concern, not merely a divergence. ``reconcile``
        proves and reaps it from the lease alone; only leases it cannot prove
        remain divergences that block the handoff.
        """
        counts = {"reclaimed": 0, "already_gone": 0, "refused_unproven": 0, "survived_kill": 0}
        inspected = 0
        possibly_alive_unproven = 0
        worker_ids: list[str] = []
        pids: list[int] = []
        release_shas: list[str] = []
        persistence_failure_ids: list[str] = []
        maintenance_failure_ids: list[str] = []
        if not read_only:
            # Maintenance, not inspection: archive terminal records so the active
            # scan stays small and the shadow converges. Failures are REPORTED,
            # never swallowed -- a collection that raises is evidence the operator
            # must see (the active scan can grow without bound if it keeps
            # failing). A read-only preflight must be side-effect free.
            if not self._try_maintenance(lambda: self._bindings.collect_terminal()):
                maintenance_failure_ids.append("binding-collect")
            leases_store = self._leases
            if leases_store is not None and not self._try_maintenance(
                lambda: leases_store.collect_terminal()
            ):
                maintenance_failure_ids.append("lease-collect")
            if not self._try_maintenance(self._lifecycle.collect_shadow_terminal):
                maintenance_failure_ids.append("shadow-collect")
        page = self._bindings.list_page()
        if not read_only:
            # P0-1: reclaim binding-less active leases from the lease alone BEFORE
            # the divergence scan, so a provable orphan whose binding is lost is
            # terminalized instead of stranding forever (the 2026-08-01 deadlock
            # shape). The scan then sees only unprovable binding-less leases as
            # divergences -- those still block, fail closed.
            binding_less_inspected, binding_less_possibly_alive = self._reclaim_binding_less_leases(
                page.records,
                departing_releases,
                counts,
                worker_ids,
                pids,
                release_shas,
                persistence_failure_ids,
            )
            inspected += binding_less_inspected
            possibly_alive_unproven += binding_less_possibly_alive
        lease_incomplete, lease_divergence, lease_scan_complete, lease_unreadable, terminal_debt = (
            scan_process_leases(self._leases, page.records)
        )
        registry_digest = digest(page.records, leases=self._leases)
        for binding in page.records:
            if binding.state not in ACTIVE_BINDING_STATES:
                continue
            inspected += 1
            if still_owned(self._owner_identity_reader, binding) and not (
                binding.release_sha is not None and binding.release_sha in departing_releases
            ):
                continue
            if not proven_execution_worker(
                self._command_line_reader, self._identity_reader, binding
            ):
                if provably_gone(self._identity_reader, self._process_group_gone, binding):
                    if read_only:
                        counts["already_gone"] += 1
                        worker_ids.append(binding.worker_id)
                        pids.append(binding.pid)
                        if binding.release_sha is not None:
                            release_shas.append(binding.release_sha)
                    else:
                        record_outcome(
                            self._lifecycle,
                            binding,
                            "already_gone",
                            counts,
                            worker_ids,
                            pids,
                            release_shas,
                            persistence_failure_ids,
                        )
                else:
                    if not read_only:
                        record_outcome(
                            self._lifecycle,
                            binding,
                            "refused_unproven",
                            counts,
                            worker_ids,
                            pids,
                            release_shas,
                            persistence_failure_ids,
                        )
                    else:
                        counts["refused_unproven"] += 1
                    possibly_alive_unproven += 1
                continue
            if read_only:
                # No side effects in preflight: a provable worker is reaped by the real
                # pass; a survived_kill lease is a blocker (previous kill failed).
                if binding.state == "survived_kill":
                    counts["survived_kill"] += 1
                continue
            outcome = self._reaper.reap(binding)
            if outcome.reaped and outcome.attempted:
                state = "reclaimed"
            elif outcome.reaped:
                state = "already_gone"
            elif outcome.still_alive:
                state = "survived_kill"
            else:
                state = "refused_unproven"
            record_outcome(
                self._lifecycle,
                binding,
                state,
                counts,
                worker_ids,
                pids,
                release_shas,
                persistence_failure_ids,
            )
        prefix = "preflight: " if read_only else ""
        return ExecutionWorkerReclamationReport(
            inspected=inspected,
            reclaimed=counts["reclaimed"],
            already_gone=counts["already_gone"],
            refused_unproven=counts["refused_unproven"],
            survived_kill=counts["survived_kill"],
            possibly_alive_unproven=possibly_alive_unproven,
            scan_complete=page.scan_complete,
            unreadable_record_ids=page.unreadable_ids,
            worker_ids=tuple(worker_ids),
            pids=tuple(pids),
            release_shas=tuple(release_shas),
            registry_digest=registry_digest,
            process_lease_incomplete=lease_incomplete,
            process_lease_binding_divergence=lease_divergence,
            process_lease_scan_complete=lease_scan_complete,
            process_lease_unreadable_ids=lease_unreadable,
            persistence_failures=len(persistence_failure_ids),
            persistence_failure_ids=tuple(persistence_failure_ids),
            maintenance_failures=len(maintenance_failure_ids),
            maintenance_failure_ids=tuple(maintenance_failure_ids),
            terminal_binding_debt=terminal_debt,
            detail=(
                f"{prefix}reconciled {inspected} live execution worker binding(s) "
                f"(scan complete: {page.scan_complete}, unreadable records: "
                f"{len(page.unreadable_ids)}): {counts['reclaimed']} reclaimed, "
                f"{counts['already_gone']} already gone, {counts['refused_unproven']} "
                f"refused unproven ({possibly_alive_unproven} possibly alive), "
                f"{counts['survived_kill']} survived kill; "
                f"process-lease incomplete={lease_incomplete}, "
                f"divergence={lease_divergence}, "
                f"persistence failures={len(persistence_failure_ids)}, "
                f"maintenance failures={len(maintenance_failure_ids)}, "
                f"terminal binding debt={terminal_debt}"
            ),
        )

    @staticmethod
    def _try_maintenance(call: Callable[[], int]) -> bool:
        """Run one best-effort maintenance sweep; report failure instead of hiding it."""
        try:
            call()
            return True
        except Exception:
            return False

    def _reclaim_binding_less_leases(
        self,
        bindings: tuple[ExecutionWorkerBinding, ...],
        departing_releases: frozenset[str],
        counts: dict[str, int],
        worker_ids: list[str],
        pids: list[int],
        release_shas: list[str],
        persistence_failure_ids: list[str],
    ) -> tuple[int, int]:
        """Reclaim binding-less active leases from the lease alone (P0-1).

        The canonical ProcessLease is the safety authority; the binding is a
        derived projection that a crash or a store failure can lose. Any lease
        that names a pid (REGISTERED/READY with a recorded pid, or any of the
        process-bearing statuses) whose binding is gone is still a real worker
        that may hold locks, so it is proven and reaped exactly like a
        binding-backed worker -- the lease carries every field the proof needs
        (pid, pgid, start token, owner identity, release sha). A pid-less fence
        lease (REGISTERED/READY with no pid) is left to the fence scan: no
        process is provable, so it stays a fail-closed incomplete concern. A
        lease that cannot be proven stays a divergence for the scan to block on
        (fail closed); a lease whose owner is alive and not departing is left
        alone.

        Returns ``(inspected, possibly_alive_unproven)`` so the caller's report
        counts binding-less concerns exactly like binding-backed ones.
        """
        if self._leases is None:
            return 0, 0
        scan = getattr(self._leases, "list_active_page", None)
        page = (
            scan(role=ProcessLeaseRole.EXECUTION_DAEMON)
            if scan is not None
            else self._leases.list_page(role=ProcessLeaseRole.EXECUTION_DAEMON)
        )
        binding_ids = {
            binding.worker_id for binding in bindings if binding.state in ACTIVE_BINDING_STATES
        }
        inspected = 0
        possibly_alive = 0
        for lease in page.records:
            if lease.lease_id in binding_ids:
                continue
            if lease.pid is None:
                # A pid-less fence lease proves no process: it stays a fence
                # member for the scan to report as incomplete (fail closed).
                continue
            inspected += 1
            concern = LeaseConcern(lease)
            if still_owned(self._owner_identity_reader, concern) and not (
                lease.release_sha is not None and lease.release_sha in departing_releases
            ):
                continue
            if not proven_execution_worker(
                self._command_line_reader, self._identity_reader, concern
            ):
                if provably_gone(self._identity_reader, self._process_group_gone, concern):
                    record_outcome(
                        self._lifecycle,
                        concern,
                        "already_gone",
                        counts,
                        worker_ids,
                        pids,
                        release_shas,
                        persistence_failure_ids,
                    )
                else:
                    record_outcome(
                        self._lifecycle,
                        concern,
                        "refused_unproven",
                        counts,
                        worker_ids,
                        pids,
                        release_shas,
                        persistence_failure_ids,
                    )
                    possibly_alive += 1
                continue
            outcome = self._reaper.reap(concern)
            if outcome.reaped and outcome.attempted:
                state = "reclaimed"
            elif outcome.reaped:
                state = "already_gone"
            elif outcome.still_alive:
                state = "survived_kill"
            else:
                state = "refused_unproven"
                possibly_alive += 1
            record_outcome(
                self._lifecycle,
                concern,
                state,
                counts,
                worker_ids,
                pids,
                release_shas,
                persistence_failure_ids,
            )
        return inspected, possibly_alive
