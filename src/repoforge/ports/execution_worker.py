"""Managed lifecycle boundary for the isolated durable execution worker."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ..domain.runtime import ChildProcess


@dataclass(frozen=True, slots=True)
class ExecutionWorkerProgressHealth:
    process_alive: bool
    heartbeat_available: bool
    heartbeat_age_seconds: float | None
    progress_healthy: bool
    loop_state: str | None
    current_operation_id: str | None
    detail: str


class ExecutionWorkerClient(Protocol):
    def start(
        self,
        generation: int,
        *,
        env: dict[str, str],
        log_path: Path,
        correlation_id: str | None = None,
    ) -> ChildProcess: ...

    def terminate(self, child: ChildProcess, *, grace_seconds: float) -> None: ...

    def is_alive(self, child: ChildProcess) -> bool: ...

    def progress_health(
        self,
        child: ChildProcess,
        *,
        now: str,
        stale_after_seconds: float,
    ) -> ExecutionWorkerProgressHealth: ...
