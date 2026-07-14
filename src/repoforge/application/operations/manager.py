"""Internal coordinator for durable operation lifecycle mutations."""

from __future__ import annotations

from ...domain.errors import ErrorCode, RepoForgeError
from ...domain.operation_task import (
    OperationCancellationDecision,
    OperationRetryability,
    OperationSnapshotBinding,
    OperationState,
    OperationTask,
    new_operation_task,
    request_operation_cancellation,
    transition_operation,
    update_operation_progress,
)
from ...ports.operation_store import OperationRecordPage
from ..context import ApplicationContext


class OperationManager:
    """Own typed operation transitions; public interfaces expose only status/list/cancel."""

    def __init__(self, ctx: ApplicationContext):
        if ctx.operation_store is None:
            raise RepoForgeError(
                "Durable operation store is not configured",
                code=ErrorCode.CONFIG_INVALID,
            )
        self.ctx = ctx
        self.store = ctx.operation_store

    def _now(self, explicit: str | None = None) -> str:
        return explicit or self.ctx.clock.now_iso()

    def status(self, operation_id: str) -> OperationTask:
        task = self.store.read(operation_id)
        if task is None:
            raise RepoForgeError(
                f"Operation not found: {operation_id}",
                code=ErrorCode.OPERATION_NOT_FOUND,
                safe_next_action="Refresh operation_list and use an exact returned operation_id.",
            )
        return task

    def list_records(self, *, max_records: int = 2_000) -> OperationRecordPage:
        return self.store.list_records(max_records=max_records)

    def create(
        self,
        *,
        kind: str,
        phase: str,
        cancel_supported: bool,
        task_id: str | None = None,
        workspace_id: str | None = None,
        snapshot_binding: OperationSnapshotBinding | None = None,
        expires_at: str | None = None,
        now: str | None = None,
    ) -> OperationTask:
        operation_id = f"op-{self.ctx.ids.new_hex(24)}"
        task = new_operation_task(
            operation_id=operation_id,
            kind=kind,
            phase=phase,
            now=self._now(now),
            cancel_supported=cancel_supported,
            task_id=task_id,
            workspace_id=workspace_id,
            snapshot_binding=snapshot_binding,
            expires_at=expires_at,
        )
        return self.ctx.audited(
            "operation_create",
            {
                "operation_id": operation_id,
                "kind": task.kind,
                "new_state": task.state.value,
                "phase": task.phase,
                "updated_at": task.updated_at,
            },
            lambda: self.store.create(task),
            mutating=True,
        )

    def _save_transition(
        self,
        operation_id: str,
        new_state: OperationState,
        *,
        now: str | None = None,
        result_reference: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        retryability: OperationRetryability = OperationRetryability.NONE,
    ) -> OperationTask:
        current = self.status(operation_id)
        updated = transition_operation(
            current,
            new_state,
            now=self._now(now),
            result_reference=result_reference,
            error_code=error_code,
            error_message=error_message,
            retryability=retryability,
        )
        if updated == current:
            return current
        return self.ctx.audited(
            "operation_transition",
            {
                "operation_id": operation_id,
                "kind": current.kind,
                "previous_state": current.state.value,
                "new_state": updated.state.value,
                "phase": updated.phase,
                "updated_at": updated.updated_at,
                "error_code": updated.error_code,
            },
            lambda: self.store.save(
                updated,
                expected_updated_at=current.updated_at,
            ),
            mutating=True,
        )

    def start(self, operation_id: str, *, now: str | None = None) -> OperationTask:
        return self._save_transition(operation_id, OperationState.RUNNING, now=now)

    def succeed(
        self,
        operation_id: str,
        *,
        result_reference: str,
        now: str | None = None,
    ) -> OperationTask:
        return self._save_transition(
            operation_id,
            OperationState.SUCCEEDED,
            now=now,
            result_reference=result_reference,
        )

    def fail(
        self,
        operation_id: str,
        *,
        error_code: str,
        error_message: str | None = None,
        retryability: OperationRetryability = OperationRetryability.NONE,
        now: str | None = None,
    ) -> OperationTask:
        return self._save_transition(
            operation_id,
            OperationState.FAILED,
            now=now,
            error_code=error_code,
            error_message=error_message,
            retryability=retryability,
        )

    def cancelled(self, operation_id: str, *, now: str | None = None) -> OperationTask:
        return self._save_transition(operation_id, OperationState.CANCELLED, now=now)

    def expire(self, operation_id: str, *, now: str | None = None) -> OperationTask:
        return self._save_transition(
            operation_id,
            OperationState.EXPIRED,
            now=now,
            error_code="OPERATION_EXPIRED",
        )

    def orphan(
        self,
        operation_id: str,
        *,
        error_code: str = "OPERATION_WORKER_LOST",
        now: str | None = None,
    ) -> OperationTask:
        return self._save_transition(
            operation_id,
            OperationState.ORPHANED,
            now=now,
            error_code=error_code,
            retryability=OperationRetryability.MANUAL,
        )

    def progress(
        self,
        operation_id: str,
        *,
        phase: str,
        current: int,
        total: int | None = None,
        unit: str | None = None,
        message: str | None = None,
        now: str | None = None,
    ) -> OperationTask:
        existing = self.status(operation_id)
        updated = update_operation_progress(
            existing,
            phase=phase,
            current=current,
            total=total,
            unit=unit,
            message=message,
            now=self._now(now),
        )
        return self.ctx.audited(
            "operation_progress",
            {
                "operation_id": operation_id,
                "kind": existing.kind,
                "state": existing.state.value,
                "phase": updated.phase,
                "progress_current": updated.progress_current,
                "progress_total": updated.progress_total,
                "updated_at": updated.updated_at,
            },
            lambda: self.store.save(
                updated,
                expected_updated_at=existing.updated_at,
            ),
            mutating=True,
        )

    def request_cancel(
        self,
        operation_id: str,
        *,
        expected_updated_at: str | None = None,
        now: str | None = None,
    ) -> OperationCancellationDecision:
        existing = self.status(operation_id)
        if expected_updated_at is not None and existing.updated_at != expected_updated_at:
            raise RepoForgeError(
                f"Operation changed since {expected_updated_at}; current updated_at is {existing.updated_at}",
                code=ErrorCode.OPERATION_STALE,
                retryable=True,
                safe_next_action="Read operation_status and retry cancellation against the refreshed updated_at.",
            )
        decision = request_operation_cancellation(existing, now=self._now(now))
        if decision.task == existing:
            return decision
        saved = self.ctx.audited(
            "operation_cancel",
            {
                "operation_id": operation_id,
                "kind": existing.kind,
                "state": existing.state.value,
                "cancellation_requested_at": decision.task.cancellation_requested_at,
                "updated_at": decision.task.updated_at,
            },
            lambda: self.store.save(
                decision.task,
                expected_updated_at=existing.updated_at,
            ),
            mutating=True,
        )
        return OperationCancellationDecision(
            saved,
            decision.cancellation_requested,
            decision.already_requested,
            decision.already_terminal,
            decision.cancel_supported,
        )

    def delete(self, operation_id: str) -> None:
        existing = self.status(operation_id)
        self.ctx.audited(
            "operation_delete",
            {
                "operation_id": operation_id,
                "kind": existing.kind,
                "state": existing.state.value,
                "updated_at": existing.updated_at,
            },
            lambda: self.store.delete(operation_id),
            mutating=True,
        )
