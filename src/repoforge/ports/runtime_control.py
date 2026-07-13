"""Protected local runtime control protocol."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from ..domain.runtime import ControlRequest, ControlResponse, RuntimeRecord


class RuntimeControlClient(Protocol):
    def request(
        self, request: ControlRequest, *, timeout_seconds: float = 10.0
    ) -> ControlResponse: ...


class RuntimeControlServer(Protocol):
    def start(self, handler: Callable[[ControlRequest], ControlResponse]) -> None: ...
    def close(self) -> None: ...


class RuntimeStore(Protocol):
    def read(self) -> RuntimeRecord | None: ...
    def write(self, record: RuntimeRecord) -> None: ...
    def clear(self, *, expected_pid: int | None = None) -> None: ...


class RuntimeLauncher(Protocol):
    def start(self, config_path: Path, *, foreground: bool, extra_env: dict[str, str]) -> int: ...
    def force_stop(self, record: RuntimeRecord, *, grace_seconds: float = 5.0) -> bool: ...
