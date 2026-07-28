"""Recoverable admission of durable operation work."""

from __future__ import annotations

from ...domain.operation_task import (
    TERMINAL_OPERATION_STATES,
    OperationSnapshotBinding,
    OperationState,
    OperationTask,
)
from ...domain.operation_work import OperationWorkRequest, new_work_item
from ...ports.operation_work_queue import OperationWorkQueue
from .manager import OperationManager


class DurableWorkAdmission:
    def __init__(
        self,
        operations: OperationManager,
        queue: OperationWorkQueue,
    ) -> None:
        self._operations = operations
        self._queue = queue

    def admit(
        self,
        request: OperationWorkRequest,
        *,
        operation_kind: str,
        expires_at: str | None = None,
    ) -> OperationTask:
        operation_id = f"op-{self._operations.ctx.ids.new_hex(24)}"
        now = self._operations.ctx.clock.now_iso()
        work = new_work_item(
            operation_id=operation_id,
            request=request,
            now=now,
        )
        self._queue.create(work)
        try:
            return self._operations.create(
                operation_id=operation_id,
                kind=operation_kind,
                phase="queued",
                cancel_supported=True,
                workspace_id=request.workspace_id,
                snapshot_binding=OperationSnapshotBinding(
                    head_sha=request.expected_head_sha,
                    workspace_fingerprint=request.expected_fingerprint,
                    config_generation=request.config_generation,
                ),
                expires_at=expires_at,
                now=now,
            )
        except Exception:
            self._queue.delete(operation_id)
            raise

    def cancel(self, operation_id: str) -> OperationTask:
        operation = self._operations.status(operation_id)
        if operation.state in TERMINAL_OPERATION_STATES:
            return operation
        if operation.state is OperationState.PENDING:
            terminal = self._operations.cancelled(operation_id)
            self._queue.delete(operation_id)
            return terminal
        self._operations.request_cancel(operation_id)
        return self._operations.status(operation_id)
