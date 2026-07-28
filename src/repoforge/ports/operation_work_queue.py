"""Durable operation-work persistence boundary."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ..domain.operation_work import OperationWorkItem


@dataclass(frozen=True, slots=True)
class OperationWorkPage:
    records: tuple[OperationWorkItem, ...]
    scan_truncated: bool


class OperationWorkQueue(Protocol):
    def create(self, item: OperationWorkItem) -> OperationWorkItem: ...

    def read(self, operation_id: str) -> OperationWorkItem | None: ...

    def save(
        self,
        item: OperationWorkItem,
        *,
        expected_updated_at: str,
    ) -> OperationWorkItem: ...

    def claim_next(
        self,
        *,
        owner_id: str,
        now: str,
        lease_expires_at: str,
        compatible_kinds: frozenset[str],
        config_generation: int | None = None,
    ) -> OperationWorkItem | None: ...

    def list_records(self, *, max_records: int) -> OperationWorkPage: ...

    def delete(self, operation_id: str) -> None: ...
