"""Thread-safe operation admission and bounded drain state."""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager

from ...domain.errors import ConfigError
from ...ports.operation_gate import GateState


class InProcessOperationGate:
    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._state = GateState.OPEN
        self._active_reads = 0
        self._active_writes = 0
        self._reason = ""
        self._correlation_id = ""

    @contextmanager
    def operation(self, operation_id: str, *, mutating: bool) -> Iterator[None]:
        del operation_id
        with self._condition:
            if self._state is GateState.FAIL_CLOSED:
                raise ConfigError(
                    f"RUNTIME_FAIL_CLOSED: {self._reason}; correlation_id={self._correlation_id}"
                )
            if self._state is GateState.DRAINING:
                raise ConfigError(
                    f"RUNTIME_RELOADING: {self._reason}; correlation_id={self._correlation_id}"
                )
            if mutating:
                self._active_writes += 1
            else:
                self._active_reads += 1
        try:
            yield
        finally:
            with self._condition:
                if mutating:
                    self._active_writes -= 1
                else:
                    self._active_reads -= 1
                self._condition.notify_all()

    def begin_drain(self, *, reason: str, correlation_id: str) -> None:
        with self._condition:
            self._state = GateState.DRAINING
            self._reason = reason
            self._correlation_id = correlation_id
            self._condition.notify_all()

    def fail_closed(self, *, reason: str, correlation_id: str) -> None:
        with self._condition:
            self._state = GateState.FAIL_CLOSED
            self._reason = reason
            self._correlation_id = correlation_id
            self._condition.notify_all()

    def reopen(self) -> None:
        with self._condition:
            self._state = GateState.OPEN
            self._reason = ""
            self._correlation_id = ""
            self._condition.notify_all()

    def wait_for_idle(self, timeout_seconds: float) -> bool:
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        with self._condition:
            while self._active_reads or self._active_writes:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(timeout=remaining)
            return True

    def snapshot(self) -> dict[str, object]:
        with self._condition:
            return {
                "state": self._state.value,
                "active_reads": self._active_reads,
                "active_writes": self._active_writes,
                "reason": self._reason,
                "correlation_id": self._correlation_id,
            }
