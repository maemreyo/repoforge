"""Bounded sleeping boundary for deterministic durable-operation tests."""

from typing import Protocol


class Sleeper(Protocol):
    def sleep(self, seconds: float) -> None: ...
