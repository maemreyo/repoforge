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
from .execution_worker_reconciler import (
    CommandLineReader,
    OwnerIdentityReader,
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
    detail: str

    def as_dict(self) -> dict[str, object]:
        return {
            "stale_execution_worker_count": self.stale_execution_worker_count,
            "workers_by_release": dict(self.workers_by_release),
            "worker_pids": list(self.worker_pids),
            "owner_supervisor_state": dict(self.owner_supervisor_state),
            "locks_held": dict(self.locks_held),
            "reclamation_safe": self.reclamation_safe,
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
) -> ExecutionWorkerReport:
    """Enumerate stale execution workers and the lock evidence against them."""

    stale: list[ExecutionWorkerBinding] = []
    owner_states: dict[str, str] = {}
    for binding in bindings.list_all():
        if binding.supervisor_process_identity is None:
            owner_states[binding.worker_id] = "unknown"
        elif owner_identity_reader(binding.supervisor_pid) == binding.supervisor_process_identity:
            owner_states[binding.worker_id] = "alive"
        else:
            owner_states[binding.worker_id] = "dead"
        if owner_states[binding.worker_id] != "alive":
            stale.append(binding)

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
        if binding.process_start_token is not None:
            identity = identity_reader(binding.pid)
            if identity is None or identity.start_token != binding.process_start_token:
                unprovable += 1

    locks_held = {
        binding.worker_id: _lock_owners(lock_root).get(binding.pid, []) for binding in stale
    }
    reclamation_safe = unprovable == 0
    return ExecutionWorkerReport(
        stale_execution_worker_count=len(stale),
        workers_by_release=workers_by_release,
        worker_pids=worker_pids,
        owner_supervisor_state=owner_states,
        locks_held=locks_held,
        reclamation_safe=reclamation_safe,
        detail=(f"{len(stale)} stale execution worker(s); reclamation safe: {reclamation_safe}"),
    )
