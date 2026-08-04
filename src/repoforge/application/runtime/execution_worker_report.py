"""Read-only execution-worker evidence for `rf doctor` / `rf runtime ls` (#368).

Reports which execution workers are stale (their owner supervisor is gone), grouped by
release, with their pids, the owner supervisor state, and which lock files claim each
stale worker's pid. Lock evidence comes from lock-file metadata plus PID identity --
never from inferring that a runtime is "stuck".
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from ...domain.execution_worker import (
    ExecutionWorkerBinding,
    is_execution_worker_entry_point,
)
from ...ports.execution_worker_store import ExecutionWorkerBindingStore
from ...ports.process_lease_store import ProcessLeaseStore
from .execution_worker_concerns import (
    ACTIVE_BINDING_STATES,
    ACTIVE_LEASE_STATUSES,
    CommandLineReader,
    OwnerIdentityReader,
    ProcessGroupGoneReader,
    ProcessIdentityReader,
)


@dataclass(frozen=True, slots=True)
class ExecutionWorkerReport:
    stale_execution_worker_count: int
    workers_by_release: dict[str, list[str]]
    worker_pids: list[int]
    owner_supervisor_state: dict[str, str]
    locks_held: dict[str, list[str]]
    reclamation_safe: bool
    scan_complete: bool
    unreadable_record_ids: tuple[str, ...]
    orphaned_group_without_leader: tuple[str, ...]
    containment_unproven: bool
    detail: str
    #: Binding-less live ProcessLease concerns (READY/RUNNING/UNPROVEN/TERMINATING
    #: ...) with no matching binding projection. These are authoritative lease
    #: concerns the doctor must surface even though the binding projection does not
    #: know them (single-authority observability).
    binding_less_lease_concerns: tuple[str, ...] = ()
    lease_scan_complete: bool = True
    lease_unreadable_ids: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        sample = self.unreadable_record_ids[:8]
        return {
            "stale_execution_worker_count": self.stale_execution_worker_count,
            "workers_by_release": dict(self.workers_by_release),
            "worker_pids": list(self.worker_pids),
            "owner_supervisor_state": dict(self.owner_supervisor_state),
            "locks_held": dict(self.locks_held),
            "reclamation_safe": self.reclamation_safe,
            "scan_complete": self.scan_complete,
            "unreadable_record_count": len(self.unreadable_record_ids),
            "unreadable_record_ids_sample": list(sample),
            "unreadable_record_ids_truncated": len(self.unreadable_record_ids) > 8,
            "orphaned_group_without_leader": list(self.orphaned_group_without_leader),
            "containment_unproven": self.containment_unproven,
            "binding_less_lease_concerns": list(self.binding_less_lease_concerns),
            "lease_scan_complete": self.lease_scan_complete,
            "lease_unreadable_count": len(self.lease_unreadable_ids),
            "lease_unreadable_ids_sample": list(self.lease_unreadable_ids[:8]),
            "lease_unreadable_ids_truncated": len(self.lease_unreadable_ids) > 8,
            "detail": self.detail,
        }


def _lock_owners(lock_root: Path) -> dict[int, list[str]]:
    """Map pid -> lock file names from lock-file metadata, best-effort."""

    owners: dict[int, list[str]] = {}
    if not lock_root.is_dir():
        return owners
    try:
        paths = sorted(lock_root.glob("*.lock"))
    except OSError:
        return owners
    for path in paths[:512]:
        try:
            raw = json.loads(path.read_text(encoding="utf-8") or "{}")
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(raw, dict):
            continue
        pid = raw.get("pid")
        if isinstance(pid, int) and not isinstance(pid, bool) and pid > 0:
            owners.setdefault(pid, []).append(path.name)
    return owners


def build_execution_worker_report(
    *,
    bindings: ExecutionWorkerBindingStore,
    lock_root: Path,
    owner_identity_reader: OwnerIdentityReader,
    command_line_reader: CommandLineReader,
    identity_reader: ProcessIdentityReader,
    process_group_gone: ProcessGroupGoneReader | None = None,
    leases: ProcessLeaseStore | None = None,
) -> ExecutionWorkerReport:
    """Enumerate stale execution workers and the lock evidence against them.

    Only active states are live concerns: terminal bindings are history, never "stale
    workers". A candidate must also still have a live process -- a binding whose
    process already exited is not a stale worker, it is a stale record (#420). But
    "leader gone" does not prove "group gone": an execution worker runs in its own
    process group, so a dead leader with live descendants is an orphaned group that
    may still hold locks, and an unavailable group probe is unprovable containment --
    both make reclamation unsafe (#424). A tokenless binding is likewise always unsafe
    (PID-reuse safety cannot be proven without the start token).

    When a ProcessLease store is wired (production), the report also surfaces
    binding-less live lease concerns: an authoritative READY/RUNNING lease with no
    matching binding projection is a real worker the doctor must show even though
    the binding table does not know it (single-authority observability).
    """

    page = bindings.list_page()
    lock_owners = _lock_owners(lock_root)
    stale: list[ExecutionWorkerBinding] = []
    owner_states: dict[str, str] = {}
    orphaned_group: list[str] = []
    containment_unproven = False
    for binding in page.records:
        if binding.state not in ACTIVE_BINDING_STATES:
            continue
        if binding.supervisor_process_identity is None:
            owner_states[binding.worker_id] = "unknown"
        elif owner_identity_reader(binding.supervisor_pid) == binding.supervisor_process_identity:
            owner_states[binding.worker_id] = "alive"
        else:
            owner_states[binding.worker_id] = "dead"
        if owner_states[binding.worker_id] == "alive":
            continue
        if identity_reader(binding.pid) is not None:
            stale.append(binding)
            continue
        # Leader is gone: is the whole group gone too?
        if process_group_gone is None:
            containment_unproven = True
            continue
        if not process_group_gone(binding.pgid):
            orphaned_group.append(binding.worker_id)
            continue
        # Leader and group are both gone: a stale historical record, not a live worker.

    workers_by_release: dict[str, list[str]] = {}
    worker_pids: list[int] = []
    unprovable = 0
    for binding in stale:
        worker_pids.append(binding.pid)
        release = binding.release_sha or "unknown"
        workers_by_release.setdefault(release, []).append(binding.worker_id)
        argv = command_line_reader(binding.pid)
        if argv is None or not is_execution_worker_entry_point(argv):
            unprovable += 1
            continue
        if binding.process_start_token is None:
            # Without the start token, PID-reuse safety can never be proven (#424).
            unprovable += 1
            continue
        identity = identity_reader(binding.pid)
        if identity is None or identity.start_token != binding.process_start_token:
            unprovable += 1

    locks_held = {binding.worker_id: lock_owners.get(binding.pid, []) for binding in stale}
    unsafe = unprovable > 0 or bool(orphaned_group) or containment_unproven

    binding_less_concerns, lease_scan_complete, lease_unreadable = _binding_less_lease_concerns(
        leases, page.records
    )
    # The verdict must be truthful about the whole evidence set: a binding-less
    # live lease is an orphan the projection cannot see, and an incomplete or
    # unreadable lease scan can hide one -- so any of those folds into
    # `reclamation_safe`. A report that says "safe" while naming binding-less
    # concerns or an incomplete lease scan would be a contradiction.
    reclamation_safe = (
        not unsafe
        and page.scan_complete
        and not page.unreadable_ids
        and not binding_less_concerns
        and lease_scan_complete
        and not lease_unreadable
    )

    return ExecutionWorkerReport(
        stale_execution_worker_count=len(stale),
        workers_by_release=workers_by_release,
        worker_pids=worker_pids,
        owner_supervisor_state=owner_states,
        locks_held=locks_held,
        reclamation_safe=reclamation_safe,
        scan_complete=page.scan_complete,
        unreadable_record_ids=page.unreadable_ids,
        orphaned_group_without_leader=tuple(orphaned_group),
        containment_unproven=containment_unproven,
        binding_less_lease_concerns=binding_less_concerns,
        lease_scan_complete=lease_scan_complete,
        lease_unreadable_ids=lease_unreadable,
        detail=(
            f"{len(stale)} stale execution worker(s); reclamation safe: "
            f"{reclamation_safe} (scan complete: {page.scan_complete}, unreadable "
            f"records: {len(page.unreadable_ids)}, orphaned groups without a leader: "
            f"{len(orphaned_group)}, containment unproven: {containment_unproven}); "
            f"binding-less lease concerns: {len(binding_less_concerns)}"
        ),
    )


def _binding_less_lease_concerns(
    leases: ProcessLeaseStore | None,
    bindings: tuple[ExecutionWorkerBinding, ...],
) -> tuple[tuple[str, ...], bool, tuple[str, ...]]:
    """Live authoritative leases with no matching active binding projection.

    A lease in an active containment state whose id has no binding -- or whose
    binding is terminal -- is a binding-less concern the doctor must show: the
    authoritative lease store knows a worker the projection does not.
    """
    if leases is None:
        return (), True, ()
    scan = getattr(leases, "list_active_page", None)
    page = scan(role=None) if scan is not None else leases.list_page(role=None)
    binding_ids = {binding.worker_id for binding in bindings}
    concerns = tuple(
        lease.lease_id
        for lease in page.records
        if lease.status in ACTIVE_LEASE_STATUSES and lease.lease_id not in binding_ids
    )
    return concerns, page.scan_complete, page.unreadable_ids
