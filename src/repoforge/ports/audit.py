"""Audit event sink boundary."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol


class AuditSink(Protocol):
    @property
    def path(self) -> Path: ...

    def record(self, action: str, *, success: bool, details: dict[str, Any]) -> None: ...
