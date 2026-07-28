"""Recoverable admission of durable operation work."""

from __future__ import annotations

from ...domain.errors import ErrorCode, RepoForgeError
from ...domain.operation_task import (
    TERMINAL_OPERATION_STATES,
    OperationRetryability,
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
        # A worker claims only work stamped with its own configuration generation, so work
        # admitted without one can never be claimed: the caller waits, and about thirty
        # seconds later recovery terminalizes it as OPERATION_GENERATION_STALE. Refusing
        # here makes the fault immediate and names it -- a process admitting durable work
        # without knowing which generation it serves is misconfigured, which is not
        # something the caller can fix by retrying (#312, after #313 was exactly this).
        if request.config_generation <= 0:
            raise RepoForgeError(
                "OPERATION_GENERATION_UNKNOWN: this process does not know which "
                "configuration generation it serves, so durable work admitted here could "
                "never be claimed by a worker.",
                code=ErrorCode.CONFIG_INVALID,
                safe_next_action=(
                    "Report this: the runtime was composed without a configuration "
                    "generation. `rf runtime restart` picks up a correctly composed "
                    "process."
                ),
            )
        operation_id = f"op-{self._operations.ctx.ids.new_hex(24)}"
        now = self._operations.ctx.clock.now_iso()
        # The operation record is written FIRST, and the work item -- the thing a worker
        # claims -- only after it. Claiming calls `OperationManager.start`, which reads
        # this record, so an entry that becomes claimable before its record exists makes
        # that read raise OPERATION_NOT_FOUND *inside the worker loop*. Observed in CI on
        # two unrelated branches: the worker thread died on exactly that and the admitted
        # work was never executed. The same window let a concurrent recovery pass see a
        # work item with no operation record and delete it as an orphan, silently dropping
        # work that had just been admitted.
        operation = self._operations.create(
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
        work = new_work_item(
            operation_id=operation_id,
            request=request,
            now=now,
        )
        try:
            self._queue.create(work)
        except Exception as exc:
            # Terminalize rather than delete: the caller already holds this operation_id,
            # so it must resolve to a record that states why nothing will run.
            self._operations.fail(
                operation_id,
                error_code="OPERATION_WORK_ADMISSION_FAILED",
                error_message=f"Durable work could not be queued: {type(exc).__name__}",
                retryability=OperationRetryability.MANUAL,
                now=now,
            )
            raise
        return operation

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
