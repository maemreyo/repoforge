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
            receipt_store = self.operations.ctx.effect_receipts
            receipt_available = (
                task.receipt_id is not None
                and receipt_store is not None
                and receipt_store.read(task.receipt_id) is not None
            )
            return operation_status_view(
                task,
                result,
                result_checked=result_store is not None,
                receipt_checked=receipt_store is not None,
                receipt_available=receipt_available,
            )

        return self.operations.ctx.audited(
            "operation_status",
            {"operation_id": command.operation_id},
            read,
        )
