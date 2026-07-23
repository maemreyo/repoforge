"""Identity-guarded termination of a detached background worker process group."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ..domain.operation_worker import OperationWorkerBinding


@dataclass(frozen=True, slots=True)
class ReapOutcome:
    """Result of attempting to terminate one bound worker process group."""

    attempted: bool
    reaped: bool
    still_alive: bool
    detail: str


class ProcessReaper(Protocol):
    def reap(self, binding: OperationWorkerBinding) -> ReapOutcome:
        """Terminate the bound process group if it is still the same process.

        Implementations must fail closed on PID reuse: a recorded pid now held by
        an unrelated process (start-token mismatch) must never be signalled.
        """
        ...

    def read_start_token(self, pid: int) -> str | None:
        """Return a stable per-process start token for PID-reuse detection.

        ``None`` when the process is gone or the host cannot report it. Used at
        bind time to record the worker's identity so a later reap can reject a
        recycled pid.
        """
        ...
