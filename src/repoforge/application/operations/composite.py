"""Consolidated durable-operation read/list/cancel orchestration for Forge v2."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from ...domain.errors import ErrorCode, RepoForgeError
from ...domain.operation_task import TERMINAL_OPERATION_STATES, OperationState
from ..workspace.failure_intelligence import FailureEvidenceReadCommand, FailureIntelligenceService
from .cancel import OperationCancelCommand, OperationCancellationRequester
from .dto import OperationStatusView, OperationSummary, operation_summary
from .list import OperationListCommand, OperationLister
from .status import OperationStatusCommand, OperationStatusReader

_ACTIONS = frozenset({"get", "wait", "list", "cancel", "failure_evidence"})


def _invalid(message: str) -> RepoForgeError:
    return RepoForgeError(
        message,
        code=ErrorCode.OPERATION_INVALID,
        safe_next_action=(
            "Use operation with action=get, wait, list, cancel, or failure_evidence and only "
            "the fields valid for that action."
        ),
    )


@dataclass(frozen=True, slots=True)
class OperationCommand:
    action: str
    operation_id: str | None = None
    scope: str | None = None
    state: str | None = None
    expected_updated_at: str | None = None
    limit: int = 50
    cursor: str | None = None
    failure_id: str | None = None
    since_updated_at: str | None = None
    timeout_seconds: int | None = None


@dataclass(frozen=True, slots=True)
class OperationResult:
    summary: str
    action: str
    operation: dict[str, object] | None
    operations: list[dict[str, object]]
    cancellation_requested: bool
    truncated: bool
    next_cursor: str | None
    failure_evidence: dict[str, object] | None = None
    changed_since: bool = False
    timed_out: bool = False


def _poll_after(view: OperationSummary | OperationStatusView) -> float | None:
    try:
        state = OperationState(view.state)
    except ValueError:
        return 2.0
    if state in TERMINAL_OPERATION_STATES:
        return None
    if view.cancellation_requested_at is not None:
        return 0.5
    if state is OperationState.PENDING:
        return 0.5
    if view.progress.total is not None and view.progress.total > 0:
        remaining = max(0, view.progress.total - view.progress.current)
        return 0.5 if remaining <= 1 else 1.0
    return 2.0


def _eta_seconds(view: OperationSummary | OperationStatusView) -> float | None:
    try:
        state = OperationState(view.state)
    except ValueError:
        return None
    if state in TERMINAL_OPERATION_STATES:
        return 0.0
    total = view.progress.total
    current = view.progress.current
    if total is None or total <= 0 or current <= 0 or current >= total:
        return None
    try:
        started = datetime.fromisoformat(view.created_at)
        updated = datetime.fromisoformat(view.updated_at)
    except ValueError:
        return None
    elapsed_seconds = max(0.0, (updated - started).total_seconds())
    if elapsed_seconds <= 0:
        return None
    remaining = total - current
    return round((elapsed_seconds / current) * remaining, 3)


def _cancellation_reason(view: OperationSummary | OperationStatusView) -> str | None:
    try:
        state = OperationState(view.state)
    except ValueError:
        return None
    if state is OperationState.CANCELLED:
        return "cancelled"
    if state is OperationState.EXPIRED:
        return "expired"
    if state is OperationState.ORPHANED:
        return view.error_code or "worker_lost"
    if view.cancellation_requested_at is not None:
        return "cancellation_requested"
    return None


def operation_evidence(view: OperationSummary | OperationStatusView) -> dict[str, object]:
    try:
        terminal = OperationState(view.state) in TERMINAL_OPERATION_STATES
    except ValueError:
        terminal = False
    return {
        "operation_id": view.operation_id,
        "kind": view.kind,
        "state": view.state,
        "phase": view.phase,
        "progress_current": view.progress.current,
        "progress_total": view.progress.total,
        "progress_unit": view.progress.unit,
        "progress_message": view.progress.message,
        "workspace_id": view.workspace_id,
        "result_reference": view.result_reference,
        "error_code": view.error_code,
        "retryability": view.retryability,
        "terminal": terminal,
        "cancellation_reason": _cancellation_reason(view),
        "poll_after_seconds": _poll_after(view),
        "suggested_poll_after_s": _poll_after(view),
        "eta_seconds": _eta_seconds(view),
        "updated_at": view.updated_at,
    }


class OperationCoordinator:
    def __init__(
        self,
        *,
        status: OperationStatusReader,
        lister: OperationLister,
        cancel: OperationCancellationRequester,
        failure_evidence: FailureIntelligenceService,
        request_live_cancel: Callable[[str, str], bool] | None = None,
    ) -> None:
        self.status = status
        self.lister = lister
        self.cancel = cancel
        self.failure_evidence = failure_evidence
        self.request_live_cancel = request_live_cancel

    def execute(self, command: OperationCommand) -> OperationResult:
        if command.action not in _ACTIONS:
            raise _invalid(f"Unknown operation action {command.action!r}")
        if command.action == "failure_evidence":
            if command.failure_id is None:
                raise _invalid("operation failure_evidence requires failure_id")
            evidence = self.failure_evidence.read(FailureEvidenceReadCommand(command.failure_id))
            return OperationResult(
                summary=f"Read failure evidence {command.failure_id}",
                action="failure_evidence",
                operation=None,
                operations=[],
                cancellation_requested=False,
                truncated=False,
                next_cursor=None,
                failure_evidence=evidence,
            )
        if command.action == "wait":
            if command.operation_id is None:
                raise _invalid("operation wait requires operation_id")
            timeout_seconds = command.timeout_seconds if command.timeout_seconds is not None else 30
            if not 1 <= timeout_seconds <= 60:
                raise _invalid("operation wait timeout_seconds must be between 1 and 60")
            current = operation_summary(self.status.operations.status(command.operation_id))
            baseline = command.since_updated_at or current.updated_at
            terminal = OperationState(current.state) in TERMINAL_OPERATION_STATES
            changed_since = command.since_updated_at is not None and current.updated_at != baseline
            deadline = time.monotonic() + timeout_seconds
            while not terminal and not changed_since:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                time.sleep(min(0.1, remaining))
                current = operation_summary(self.status.operations.status(command.operation_id))
                terminal = OperationState(current.state) in TERMINAL_OPERATION_STATES
                changed_since = current.updated_at != baseline
            timed_out = not terminal and not changed_since
            return OperationResult(
                summary=(
                    f"Operation {command.operation_id} reached terminal state"
                    if terminal
                    else (
                        f"Operation {command.operation_id} advanced"
                        if changed_since
                        else f"Operation {command.operation_id} wait timed out"
                    )
                ),
                action="wait",
                operation=operation_evidence(current),
                operations=[],
                cancellation_requested=False,
                truncated=False,
                next_cursor=None,
                changed_since=changed_since,
                timed_out=timed_out,
            )
        if command.action == "get":
            if command.operation_id is None:
                raise _invalid("operation get requires operation_id")
            view = self.status.execute(OperationStatusCommand(command.operation_id))
            return OperationResult(
                summary=f"Read durable operation {view.operation_id}",
                action="get",
                operation=operation_evidence(view),
                operations=[],
                cancellation_requested=False,
                truncated=False,
                next_cursor=None,
            )
        if command.action == "list":
            page = self.lister.execute(
                OperationListCommand(
                    scope=command.scope,
                    state=command.state,
                    limit=command.limit,
                    cursor=command.cursor,
                )
            )
            return OperationResult(
                summary=f"Listed {len(page.operations)} durable operations",
                action="list",
                operation=None,
                operations=[operation_evidence(item) for item in page.operations],
                cancellation_requested=False,
                truncated=page.scan_truncated or page.next_cursor is not None,
                next_cursor=page.next_cursor,
            )
        if command.operation_id is None:
            raise _invalid("operation cancel requires operation_id")
        decision = self.cancel.execute(
            OperationCancelCommand(command.operation_id, command.expected_updated_at)
        )
        if decision.cancellation_requested and self.request_live_cancel is not None:
            self.request_live_cancel(decision.operation.kind, command.operation_id)
        return OperationResult(
            summary=(
                f"Requested cancellation for {command.operation_id}"
                if decision.cancellation_requested
                else f"Cancellation state is unchanged for {command.operation_id}"
            ),
            action="cancel",
            operation=operation_evidence(decision.operation),
            operations=[],
            cancellation_requested=decision.cancellation_requested,
            truncated=False,
            next_cursor=None,
        )


__all__ = [
    "OperationCommand",
    "OperationCoordinator",
    "OperationResult",
    "operation_evidence",
]
