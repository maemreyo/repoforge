"""Durable operation persistence boundary."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ..domain.operation_task import OperationTask


@dataclass(frozen=True, slots=True)
class OperationRecordPage:
    records: tuple[OperationTask, ...]
    scan_truncated: bool


class OperationStore(Protocol):
    def create(self, task: OperationTask) -> OperationTask: ...

    def read(self, operation_id: str) -> OperationTask | None: ...

    def save(self, task: OperationTask, *, expected_updated_at: str) -> OperationTask: ...

    def list_records(self, *, max_records: int) -> OperationRecordPage: ...

    def delete(self, operation_id: str) -> None: ...
