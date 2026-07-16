"""Private durable storage boundary for bounded operation results."""

from __future__ import annotations

from typing import Any, Protocol


class OperationResultStore(Protocol):
    def save(self, operation_id: str, result: dict[str, Any]) -> None: ...

    def read(self, operation_id: str) -> dict[str, Any] | None: ...

    def delete(self, operation_id: str) -> None: ...
