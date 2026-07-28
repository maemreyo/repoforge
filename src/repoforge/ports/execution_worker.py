"""Managed lifecycle boundary for the isolated durable execution worker."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from ..domain.runtime import ChildProcess


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
