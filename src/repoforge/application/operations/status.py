"""Read one durable operation by exact ID."""

from __future__ import annotations

from dataclasses import dataclass

from .dto import OperationStatusView, operation_status_view
from .manager import OperationManager


@dataclass(frozen=True, slots=True)
class OperationStatusCommand:
    operation_id: str


class OperationStatusReader:
    def __init__(self, operations: OperationManager):
        self.operations = operations

    def execute(self, command: OperationStatusCommand) -> OperationStatusView:
        def read() -> OperationStatusView:
            task = self.operations.status(command.operation_id)
            result = None
            result_store = self.operations.ctx.operation_result_store
            if result_store is not None:
                result = result_store.read(command.operation_id)
            return operation_status_view(task, result)

        return self.operations.ctx.audited(
            "operation_status",
            {"operation_id": command.operation_id},
            read,
        )
