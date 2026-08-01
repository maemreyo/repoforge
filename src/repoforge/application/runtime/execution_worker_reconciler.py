"""PID-safe reconciliation of orphaned execution workers (#368).

An execution worker is spawned by a supervisor with ``start_new_session=True`` and is
tracked only in the supervisor's RAM. When the supervisor dies, the worker is
reparented and keeps running -- holding operation locks that block the next release
from converging (92 workers in the 2026-08-01 incident). This reconciler reclaims
them before a new supervisor starts, proving each worker's identity first:

1. the owner supervisor must be dead, or the worker must belong to a departing
   release -- a worker owned by a live supervisor is never touched;
2. the process must still be the same one (start-token match, PID-reuse guard);
3. its command line must be exactly the execution-worker entry point -- nothing else
   running from an old release is ever killed by pattern;
4. only then is the process group TERM->KILL reaped with a bounded wait.

Any unprovable step fails closed: the worker is marked ``refused_unproven`` and left
running rather than signalled on a guess.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from ...domain.execution_worker import (
    ExecutionWorkerBinding,
    is_execution_worker_entry_point,
)
from ...ports.execution_worker_store import ExecutionWorkerBindingStore
from ...ports.process_reaper import ProcessReaper


class ProcessIdentityLike(Protocol):
    """The one field the reconciler reads off a process identity: the start token."""

    @property
    def start_token(self) -> str | None: ...


OwnerIdentityReader = Callable[[int], str | None]
CommandLineReader = Callable[[int], tuple[str, ...] | None]
ProcessIdentityReader = Callable[[int], ProcessIdentityLike | None]


@dataclass(frozen=True, slots=True)
class ExecutionWorkerReclamationReport:
    """Bounded evidence of one reconciliation pass, for receipts and doctor output."""

    inspected: int
    reclaimed: int
    already_gone: int
    refused_unproven: int
    survived_kill: int
    worker_ids: tuple[str, ...]
    pids: tuple[int, ...]
    release_shas: tuple[str, ...]
    detail: str

    def as_dict(self) -> dict[str, object]:
        return {
            "inspected": self.inspected,
            "reclaimed": self.reclaimed,
            "already_gone": self.already_gone,
            "refused_unproven": self.refused_unproven,
            "survived_kill": self.survived_kill,
            "worker_ids": list(self.worker_ids),
            "pids": list(self.pids),
            "release_shas": list(self.release_shas),
            "detail": self.detail,
        }


class ExecutionWorkerReconciler:
    def __init__(
        self,
        *,
        bindings: ExecutionWorkerBindingStore,
        reaper: ProcessReaper,
        owner_identity_reader: OwnerIdentityReader,
        command_line_reader: CommandLineReader,
        identity_reader: ProcessIdentityReader,
    ) -> None:
        self._bindings = bindings
        self._reaper = reaper
        self._owner_identity_reader = owner_identity_reader
        self._command_line_reader = command_line_reader
        self._identity_reader = identity_reader

    def reconcile(
        self, *, departing_releases: frozenset[str] = frozenset()
    ) -> ExecutionWorkerReclamationReport:
        """Reclaim orphaned/departing execution workers, bounded and evidence-backed."""

        counts = {"reclaimed": 0, "already_gone": 0, "refused_unproven": 0, "survived_kill": 0}
        inspected = 0
        worker_ids: list[str] = []
        pids: list[int] = []
        release_shas: list[str] = []
        for binding in self._bindings.list_all():
            inspected += 1
            if self._still_owned(binding) and not (
                binding.release_sha is not None and binding.release_sha in departing_releases
            ):
                continue
            if not self._proven_execution_worker(binding):
                self._mark(binding.worker_id, "refused_unproven")
                counts["refused_unproven"] += 1
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
            self._mark(binding.worker_id, state)
            counts[state] += 1
            if state in {"reclaimed", "already_gone"}:
                worker_ids.append(binding.worker_id)
                pids.append(binding.pid)
                if binding.release_sha is not None:
                    release_shas.append(binding.release_sha)
        return ExecutionWorkerReclamationReport(
            inspected=inspected,
            reclaimed=counts["reclaimed"],
            already_gone=counts["already_gone"],
            refused_unproven=counts["refused_unproven"],
            survived_kill=counts["survived_kill"],
            worker_ids=tuple(worker_ids),
            pids=tuple(pids),
            release_shas=tuple(release_shas),
            detail=(
                f"reconciled {inspected} execution worker binding(s): "
                f"{counts['reclaimed']} reclaimed, {counts['already_gone']} already gone, "
                f"{counts['refused_unproven']} refused unproven, "
                f"{counts['survived_kill']} survived kill"
            ),
        )

    def _still_owned(self, binding: ExecutionWorkerBinding) -> bool:
        """Is the recorded owner supervisor alive and still the same process?"""

        if binding.supervisor_process_identity is None:
            return False
        return (
            self._owner_identity_reader(binding.supervisor_pid)
            == binding.supervisor_process_identity
        )

    def _proven_execution_worker(self, binding: ExecutionWorkerBinding) -> bool:
        """Prove the process is still the same worker with the exact entry point."""

        argv = self._command_line_reader(binding.pid)
        if argv is None:
            return False
        if not is_execution_worker_entry_point(argv):
            return False
        if binding.process_start_token is not None:
            identity = self._identity_reader(binding.pid)
            if identity is None or identity.start_token != binding.process_start_token:
                return False
        return True

    def _mark(self, worker_id: str, state: str) -> None:
        with contextlib.suppress(Exception):
            self._bindings.update_state(worker_id, state)
