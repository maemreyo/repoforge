"""Idempotency record persistence boundary."""

from __future__ import annotations

from typing import Protocol

from ..domain.operations import IdempotencyRecord


class IdempotencyStore(Protocol):
    def load(self, action: str, key_hash: str) -> IdempotencyRecord | None: ...

    def save(self, record: IdempotencyRecord) -> None: ...

    def delete(self, action: str, key_hash: str) -> None: ...
