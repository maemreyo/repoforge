"""Time boundary for deterministic application tests."""

from typing import Protocol


class Clock(Protocol):
    def now_iso(self) -> str: ...
