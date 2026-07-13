"""Cross-process mutual exclusion boundary."""

from __future__ import annotations

from contextlib import AbstractContextManager
from pathlib import Path
from typing import Protocol


class LockManager(Protocol):
    def lock(
        self,
        name: str,
        *,
        timeout_seconds: float | None = None,
        metadata: dict[str, str] | None = None,
    ) -> AbstractContextManager[None]: ...

    def path_for(self, name: str) -> Path: ...
