"""Request cancellation for one durable operation."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from ...domain.operation_task import OperationState
from ...ports.operation_work_queue import OperationWorkQueue
from ...ports.process_reaper import ProcessReaper
from ...ports.worker_binding_store import WorkerBindingStore
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
    def __init__(
        self,
        operations: OperationManager,
        work_queue: OperationWorkQueue | None = None,
        worker_bindings: WorkerBindingStore | None = None,
        reaper: ProcessReaper | None = None,
        request_live_cancel: Callable[[str, str], bool] | None = None,
    ):
        self.operations = operations
        self.work_queue = work_queue
        self.worker_bindings = worker_bindings
        self.reaper = reaper
        self.request_live_cancel = request_live_cancel

    def _signalled_live_owner(self, kind: str, operation_id: str) -> bool:
        if self.request_live_cancel is None:
            return False
        return self.request_live_cancel(kind, operation_id)

    def execute(self, command: OperationCancelCommand) -> OperationCancelResult:
        decision = self.operations.request_cancel(
            command.operation_id,
            expected_updated_at=command.expected_updated_at,
        )
        task = decision.task
        if (
            self.work_queue is not None
            and task.state is OperationState.PENDING
            and decision.cancel_supported
            and self.work_queue.read(command.operation_id) is not None
        ):
            task = self.operations.cancelled(command.operation_id)
            self.work_queue.delete(command.operation_id)
        elif (
            task.state is OperationState.RUNNING
            and decision.cancellation_requested
            and self._signalled_live_owner(task.kind, command.operation_id)
        ):
            # An execution owned by this process cancels through its own token, so
            # the run itself records the cancellation in its result and audit trail
            # and terminalizes the operation. Reaping the group from here instead
            # would leave the owner reporting an ordinary command failure.
            pass
        elif (
            task.state is OperationState.RUNNING
            and decision.cancellation_requested
            and self.worker_bindings is not None
            and self.reaper is not None
        ):
            binding = self.worker_bindings.get(command.operation_id)
            if binding is not None:
                outcome = self.reaper.reap(binding)
                if outcome.reaped and not outcome.still_alive:
                    task = self.operations.cancelled(
                        command.operation_id,
                        owner_id=task.owner_id,
                    )
                    if self.work_queue is not None:
                        self.work_queue.delete(command.operation_id)
                    self.worker_bindings.delete_if_unchanged(binding)
        return OperationCancelResult(
            operation=operation_summary(task),
            cancellation_requested=decision.cancellation_requested,
            already_requested=decision.already_requested,
            already_terminal=decision.already_terminal,
            cancel_supported=decision.cancel_supported,
        )
