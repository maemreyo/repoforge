"""Request cancellation for one durable operation."""

from __future__ import annotations

from dataclasses import dataclass

from .dto import OperationSummary, operation_summary
from .manager import OperationManager


@dataclass(frozen=True, slots=True)
class OperationCancelCommand:
    operation_id: str
    expected_updated_at: str | None = None


@dataclass(frozen=True, slots=True)
class OperationCancelResult:
    operation: OperationSummary
    cancellation_requested: bool
    already_requested: bool
    already_terminal: bool
    cancel_supported: bool


class OperationCancellationRequester:
    def __init__(self, operations: OperationManager):
        self.operations = operations

    def execute(self, command: OperationCancelCommand) -> OperationCancelResult:
        decision = self.operations.request_cancel(
            command.operation_id,
            expected_updated_at=command.expected_updated_at,
        )
        return OperationCancelResult(
            operation=operation_summary(decision.task),
            cancellation_requested=decision.cancellation_requested,
            already_requested=decision.already_requested,
            already_terminal=decision.already_terminal,
            cancel_supported=decision.cancel_supported,
        )
