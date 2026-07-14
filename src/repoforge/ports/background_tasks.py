"""Background task scheduling boundary for durable local operations."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol


class BackgroundTaskRunner(Protocol):
    def submit(self, key: str, task: Callable[[], None]) -> bool: ...
