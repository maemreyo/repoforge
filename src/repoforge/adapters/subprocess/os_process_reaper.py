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
from .process_tree import ProcessIdentity, group_has_live_member, read_identity


def _process_group_exists(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class OsProcessReaper:
    def __init__(
        self,
        *,
        identity_reader: Callable[[int], ProcessIdentity | None] = read_identity,
        killpg: Callable[[int, int], None] = os.killpg,
        group_exists: Callable[[int], bool] = _process_group_exists,
        live_member_probe: Callable[[int], bool | None] = group_has_live_member,
        sleeper: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        term_grace_seconds: float = 2.0,
    ) -> None:
        self._identity_reader = identity_reader
        self._killpg = killpg
        self._group_exists = group_exists
        self._live_member_probe = live_member_probe
        self._sleeper = sleeper
        self._monotonic = monotonic
        self._term_grace_seconds = term_grace_seconds

    def read_start_token(self, pid: int) -> str | None:
        if pid <= 0:
            return None
        identity = self._identity_reader(pid)
        return identity.start_token if identity is not None else None

    def _gone(self, binding: OperationWorkerBinding) -> bool:
        """Report whether nothing in the bound group can still execute.

        `killpg(pgid, 0)` succeeds for a group whose members have all stopped and
        are only waiting to be reaped by their parent, so the signal probe alone
        reports a successfully killed child as having survived. The cheap probes
        stay in the polling path: the group signal first, then the bound leader's
        own identity. Only once the leader has stopped is the whole group
        enumerated, and an uninspectable host still fails closed as alive.
        """
        if not self._group_exists(binding.child_pgid):
            return True
        if self._identity_reader(binding.child_pid) is not None:
            return False
        return self._live_member_probe(binding.child_pgid) is False

    def _signal_group(self, pgid: int, sig: int) -> None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            self._killpg(pgid, sig)

    def reap(self, binding: OperationWorkerBinding) -> ReapOutcome:
        current = self._identity_reader(binding.child_pid)
        group_exists = self._group_exists(binding.child_pgid)
        if current is None:
            if not group_exists:
                return ReapOutcome(
                    attempted=False,
                    reaped=True,
                    still_alive=False,
                    detail="child process group already gone",
                )
            return ReapOutcome(
                attempted=False,
                reaped=False,
                still_alive=True,
                detail="process-group leader is gone; containment identity is unproven",
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
