"""Idempotency record persistence boundary."""

from __future__ import annotations

from contextlib import AbstractContextManager
from typing import Protocol

from ..domain.operations import IdempotencyRecord


class IdempotencyStore(Protocol):
    def transaction(
        self,
        action: str,
        key_hash: str,
        *,
        timeout_seconds: float,
        metadata: dict[str, str] | None = None,
    ) -> AbstractContextManager[None]: ...

    def load(self, action: str, key_hash: str) -> IdempotencyRecord | None: ...

    def save(self, record: IdempotencyRecord) -> None: ...

    def delete(self, action: str, key_hash: str) -> None: ...
