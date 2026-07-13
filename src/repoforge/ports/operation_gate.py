"""Runtime drain/fail-closed coordination boundary."""

from __future__ import annotations

from contextlib import AbstractContextManager
from enum import Enum
from typing import Protocol


class GateState(str, Enum):
    OPEN = "open"
    DRAINING = "draining"
    FAIL_CLOSED = "fail_closed"


class OperationGate(Protocol):
    def operation(self, operation_id: str, *, mutating: bool) -> AbstractContextManager[None]: ...

    def begin_drain(self, *, reason: str, correlation_id: str) -> None: ...
    def fail_closed(self, *, reason: str, correlation_id: str) -> None: ...
    def reopen(self) -> None: ...
    def wait_for_idle(self, timeout_seconds: float) -> bool: ...
    def snapshot(self) -> dict[str, object]: ...
