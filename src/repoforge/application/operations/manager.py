"""Internal coordinator for durable operation lifecycle mutations."""

from __future__ import annotations

import contextlib

from ...domain.errors import ErrorCode, RepoForgeError
from ...domain.operation_task import (
    OperationCancellationDecision,
    OperationRetryability,
    OperationSnapshotBinding,
    OperationState,
    OperationTask,
    claim_operation_ownership,
    new_operation_task,
    renew_operation_ownership,
    request_operation_cancellation,
    require_operation_owner,
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
        receipt_id: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        retryability: OperationRetryability = OperationRetryability.NONE,
        owner_id: str | None = None,
        lease_expires_at: str | None = None,
        bypass_ownership: bool = False,
    ) -> OperationTask:
        current = self.status(operation_id)
        transition_now = self._now(now)
        if new_state is not OperationState.RUNNING:
            if lease_expires_at is not None:
                raise RepoForgeError(
                    "lease_expires_at is valid only when starting an operation",
                    code=ErrorCode.OPERATION_INVALID,
                )
            if not bypass_ownership:
                require_operation_owner(current, owner_id)
        updated = transition_operation(
            current,
            new_state,
            now=transition_now,
            result_reference=result_reference,
            receipt_id=receipt_id,
            error_code=error_code,
            error_message=error_message,
            retryability=retryability,
        )
        if new_state is OperationState.RUNNING and (
            owner_id is not None or lease_expires_at is not None
        ):
            if owner_id is None or lease_expires_at is None:
                raise RepoForgeError(
                    "owner_id and lease_expires_at must be provided together",
                    code=ErrorCode.OPERATION_INVALID,
                )
            updated = claim_operation_ownership(
                updated,
                owner_id=owner_id,
                lease_expires_at=lease_expires_at,
                now=transition_now,
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

    def start(
        self,
        operation_id: str,
        *,
        owner_id: str | None = None,
        lease_expires_at: str | None = None,
        now: str | None = None,
    ) -> OperationTask:
        return self._save_transition(
            operation_id,
            OperationState.RUNNING,
            now=now,
            owner_id=owner_id,
            lease_expires_at=lease_expires_at,
        )

    def succeed(
        self,
        operation_id: str,
        *,
        result_reference: str,
        receipt_id: str | None = None,
        owner_id: str | None = None,
        now: str | None = None,
    ) -> OperationTask:
        return self._save_transition(
            operation_id,
            OperationState.SUCCEEDED,
            now=now,
            result_reference=result_reference,
            receipt_id=receipt_id,
            owner_id=owner_id,
        )

    def fail(
        self,
        operation_id: str,
        *,
        error_code: str,
        error_message: str | None = None,
        result_reference: str | None = None,
        receipt_id: str | None = None,
        retryability: OperationRetryability = OperationRetryability.NONE,
        owner_id: str | None = None,
        now: str | None = None,
    ) -> OperationTask:
        return self._save_transition(
            operation_id,
            OperationState.FAILED,
            now=now,
            result_reference=result_reference,
            receipt_id=receipt_id,
            error_code=error_code,
            error_message=error_message,
            retryability=retryability,
            owner_id=owner_id,
        )

    def cancelled(
        self,
        operation_id: str,
        *,
        owner_id: str | None = None,
        now: str | None = None,
    ) -> OperationTask:
        return self._save_transition(
            operation_id,
            OperationState.CANCELLED,
            now=now,
            owner_id=owner_id,
        )

    def expire(self, operation_id: str, *, now: str | None = None) -> OperationTask:
        return self._save_transition(
            operation_id,
            OperationState.EXPIRED,
            now=now,
            error_code="OPERATION_EXPIRED",
            bypass_ownership=True,
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
            bypass_ownership=True,
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
        owner_id: str | None = None,
        lease_expires_at: str | None = None,
        now: str | None = None,
    ) -> OperationTask:
        existing = self.status(operation_id)
        require_operation_owner(existing, owner_id)
        progress_now = self._now(now)
        updated = update_operation_progress(
            existing,
            phase=phase,
            current=current,
            total=total,
            unit=unit,
            message=message,
            now=progress_now,
        )
        if lease_expires_at is not None:
            if owner_id is None:
                raise RepoForgeError(
                    "lease renewal requires owner_id",
                    code=ErrorCode.OPERATION_INVALID,
                )
            updated = renew_operation_ownership(
                updated,
                owner_id=owner_id,
                lease_expires_at=lease_expires_at,
                now=progress_now,
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

    def renew_ownership(
        self,
        operation_id: str,
        *,
        owner_id: str,
        lease_expires_at: str,
        now: str | None = None,
    ) -> OperationTask:
        existing = self.status(operation_id)
        updated = renew_operation_ownership(
            existing,
            owner_id=owner_id,
            lease_expires_at=lease_expires_at,
            now=self._now(now),
        )
        return self.ctx.audited(
            "operation_ownership_renew",
            {
                "operation_id": operation_id,
                "kind": existing.kind,
                "owner_id": updated.owner_id,
                "lease_expires_at": updated.lease_expires_at,
                "updated_at": updated.updated_at,
            },
            lambda: self.store.save(updated, expected_updated_at=existing.updated_at),
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

        def delete() -> None:
            self.store.delete(operation_id)
            if self.ctx.operation_result_store is not None:
                with contextlib.suppress(RepoForgeError):
                    self.ctx.operation_result_store.delete(operation_id)

        self.ctx.audited(
            "operation_delete",
            {
                "operation_id": operation_id,
                "kind": existing.kind,
                "state": existing.state.value,
                "updated_at": existing.updated_at,
            },
            delete,
            mutating=True,
        )
