"""Read one durable operation by exact ID."""

from __future__ import annotations

from dataclasses import dataclass

from .dto import OperationSummary, operation_summary
from .manager import OperationManager


@dataclass(frozen=True, slots=True)
class OperationStatusCommand:
    operation_id: str


class OperationStatusReader:
    def __init__(self, operations: OperationManager):
        self.operations = operations

    def execute(self, command: OperationStatusCommand) -> OperationSummary:
        return self.operations.ctx.audited(
            "operation_status",
            {"operation_id": command.operation_id},
            lambda: operation_summary(self.operations.status(command.operation_id)),
        )
