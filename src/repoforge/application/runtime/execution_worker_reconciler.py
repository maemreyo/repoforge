"""PID-safe reconciliation of orphaned execution workers (#368).

An execution worker is spawned by a supervisor with ``start_new_session=True`` and is
tracked only in the supervisor's RAM. When the supervisor dies, the worker is
reparented and keeps running -- holding operation locks that block the next release
from converging (92 workers in the 2026-08-01 incident). This reconciler reclaims
them before a new supervisor starts, proving each worker's identity first:

1. only ``running`` bindings are live concerns -- terminal bindings are history;
2. the owner supervisor must be dead, or the worker must belong to a departing
   release -- a worker owned by a live supervisor is never touched;
3. the binding must carry a process start token (PID-reuse proof) and the process
   must still be the same one;
4. its command line must be exactly the execution-worker entry point -- nothing else
   running from an old release is ever killed by pattern;
5. only then is the process group TERM->KILL reaped with a bounded wait.

Fail-closed policy (#420): an unclassifiable worker whose process AND group are
provably gone is ``already_gone`` (safe); one that may still be running is
``refused_unproven`` and counted as ``possibly_alive_unproven`` so the caller can
block starting a replacement -- not killing is not safety. An incomplete registry
scan (``scan_complete=False``) is likewise a block, because an orphan past the scan
limit would be invisible.
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
from ...ports.execution_worker_store import (
    ExecutionWorkerBindingStore,
)
from ...ports.process_reaper import ProcessReaper


class ProcessIdentityLike(Protocol):
    """The one field the reconciler reads off a process identity: the start token."""

    @property
    def start_token(self) -> str | None: ...


OwnerIdentityReader = Callable[[int], str | None]
CommandLineReader = Callable[[int], tuple[str, ...] | None]
ProcessIdentityReader = Callable[[int], ProcessIdentityLike | None]
ProcessGroupGoneReader = Callable[[int], bool]

_ACTIVE_STATES = frozenset({"running"})


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
            "possibly_alive_unproven": self.possibly_alive_unproven,
            "scan_complete": self.scan_complete,
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
        process_group_gone: ProcessGroupGoneReader | None = None,
    ) -> None:
        self._bindings = bindings
        self._reaper = reaper
        self._owner_identity_reader = owner_identity_reader
        self._command_line_reader = command_line_reader
        self._identity_reader = identity_reader
        self._process_group_gone = process_group_gone

    def reconcile(
        self, *, departing_releases: frozenset[str] = frozenset()
    ) -> ExecutionWorkerReclamationReport:
        """Reclaim orphaned/departing execution workers, bounded and evidence-backed."""

        counts = {"reclaimed": 0, "already_gone": 0, "refused_unproven": 0, "survived_kill": 0}
        inspected = 0
        possibly_alive_unproven = 0
        worker_ids: list[str] = []
        pids: list[int] = []
        release_shas: list[str] = []
        page = self._bindings.list_page()
        for binding in page.records:
            if binding.state not in _ACTIVE_STATES:
                continue
            inspected += 1
            if self._still_owned(binding) and not (
                binding.release_sha is not None and binding.release_sha in departing_releases
            ):
                continue
            if not self._proven_execution_worker(binding):
                if self._provably_gone(binding):
                    self._mark(binding.worker_id, "already_gone")
                    counts["already_gone"] += 1
                    worker_ids.append(binding.worker_id)
                    pids.append(binding.pid)
                    if binding.release_sha is not None:
                        release_shas.append(binding.release_sha)
                else:
                    self._mark(binding.worker_id, "refused_unproven")
                    counts["refused_unproven"] += 1
                    possibly_alive_unproven += 1
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
            possibly_alive_unproven=possibly_alive_unproven,
            scan_complete=page.scan_complete,
            worker_ids=tuple(worker_ids),
            pids=tuple(pids),
            release_shas=tuple(release_shas),
            detail=(
                f"reconciled {inspected} running execution worker binding(s) "
                f"(scan complete: {page.scan_complete}): {counts['reclaimed']} reclaimed, "
                f"{counts['already_gone']} already gone, {counts['refused_unproven']} "
                f"refused unproven ({possibly_alive_unproven} possibly alive), "
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
        """Prove the process is still the same worker with the exact entry point.

        A binding without a start token can never prove PID-reuse safety, so it is
        never auto-reaped (#420): the check is skipped only when the token is absent
        and the result is refuse, never signal.
        """

        if binding.process_start_token is None:
            return False
        argv = self._command_line_reader(binding.pid)
        if argv is None:
            return False
        if not is_execution_worker_entry_point(argv):
            return False
        identity = self._identity_reader(binding.pid)
        return not (identity is None or identity.start_token != binding.process_start_token)

    def _provably_gone(self, binding: ExecutionWorkerBinding) -> bool:
        """Is the process AND its group proven absent, so no signal is needed?

        ``False`` (not provably gone) is the conservative answer: an unprovable group
        must be treated as possibly alive and block a replacement start.
        """

        if self._identity_reader(binding.pid) is not None:
            return False
        if self._process_group_gone is None:
            return False
        return self._process_group_gone(binding.pgid)

    def _mark(self, worker_id: str, state: str) -> None:
        with contextlib.suppress(Exception):
            self._bindings.update_state(worker_id, state)
