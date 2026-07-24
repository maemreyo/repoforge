"""OS-backed, PID-reuse-safe reaper for detached background worker groups.

Mirrors the process-group termination the subprocess timeout path already uses
(``SIGTERM`` escalating to ``SIGKILL``); it does not invent a second mechanism.
Because a background command is spawned with ``start_new_session=True`` its pgid
equals its own pid, so signalling ``killpg(pgid)`` reaches the whole reparented
subtree even after the process that launched it has died.

The OS calls are injectable so the reaping decision logic is testable without
real processes.
"""

from __future__ import annotations

import contextlib
import os
import signal
import time
from collections.abc import Callable

from ...domain.operation_worker import OperationWorkerBinding
from ...ports.process_reaper import ReapOutcome
from .process_tree import ProcessIdentity, read_identity


class OsProcessReaper:
    def __init__(
        self,
        *,
        identity_reader: Callable[[int], ProcessIdentity | None] = read_identity,
        killpg: Callable[[int, int], None] = os.killpg,
        sleeper: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        term_grace_seconds: float = 2.0,
    ) -> None:
        self._identity_reader = identity_reader
        self._killpg = killpg
        self._sleeper = sleeper
        self._monotonic = monotonic
        self._term_grace_seconds = term_grace_seconds

    def read_start_token(self, pid: int) -> str | None:
        if pid <= 0:
            return None
        identity = self._identity_reader(pid)
        return identity.start_token if identity is not None else None

    def _gone(self, binding: OperationWorkerBinding) -> bool:
        return self._identity_reader(binding.child_pid) is None

    def _signal_group(self, pgid: int, sig: int) -> None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            self._killpg(pgid, sig)

    def reap(self, binding: OperationWorkerBinding) -> ReapOutcome:
        current = self._identity_reader(binding.child_pid)
        if current is None:
            return ReapOutcome(
                attempted=False,
                reaped=True,
                still_alive=False,
                detail="child already gone",
            )
        if (
            binding.child_start_token is not None
            and current.start_token != binding.child_start_token
        ):
            return ReapOutcome(
                attempted=False,
                reaped=False,
                still_alive=False,
                detail="pid reused by unrelated process; not signalled",
            )
        self._signal_group(binding.child_pgid, signal.SIGTERM)
        deadline = self._monotonic() + max(0.0, self._term_grace_seconds)
        while self._monotonic() < deadline:
            if self._gone(binding):
                return ReapOutcome(
                    attempted=True,
                    reaped=True,
                    still_alive=False,
                    detail="reaped via SIGTERM",
                )
            self._sleeper(0.05)
        self._signal_group(binding.child_pgid, signal.SIGKILL)
        self._sleeper(0.1)
        gone = self._gone(binding)
        return ReapOutcome(
            attempted=True,
            reaped=gone,
            still_alive=not gone,
            detail="reaped via SIGKILL" if gone else "survived SIGKILL",
        )
