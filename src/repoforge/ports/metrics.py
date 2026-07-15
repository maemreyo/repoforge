"""Bounded local operation-metrics boundary."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol


class MetricsSink(Protocol):
    @property
    def path(self) -> Path: ...

    def record(
        self,
        action: str,
        *,
        success: bool,
        duration_ms: float,
        error_code: str | None,
        result_bytes: int | None = None,
    ) -> None: ...

    def snapshot(self) -> dict[str, Any]: ...
