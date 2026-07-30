"""Consolidated durable-operation read/list/cancel orchestration for Forge v2."""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime

from ...domain.errors import ErrorCode, RepoForgeError
from ...domain.operation_task import TERMINAL_OPERATION_STATES, OperationState
from ...ports.progress_reporter import ProgressReporter
from ..workspace.failure_intelligence import FailureEvidenceReadCommand, FailureIntelligenceService
from .cancel import OperationCancelCommand, OperationCancellationRequester
from .dto import OperationStatusView, OperationSummary
from .list import OperationListCommand, OperationLister
from .progress_context import current_progress_reporter
from .status import OperationStatusCommand, OperationStatusReader

_ACTIONS = frozenset({"get", "wait", "list", "cancel", "failure_evidence"})
_WAIT_UNTIL = frozenset({"progress", "terminal"})
# The ceiling is bounded by the client, not by RepoForge: a held request dies with the
# connector, and this codebase has watched that happen. Progress notifications keep the
# request alive while work continues, and `since_updated_at` makes a dropped wait
# resumable, so 300 seconds is the point where one call covers most gates without
# betting the answer on a five-minute-plus tunnel.
_MAX_WAIT_SECONDS = 300


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
    until: str = "progress"


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
    # Wait-only. `progress_delivery` tells the caller which mechanism it actually got, and
    # the two resubscribe fields are the only way to continue a wait that returned no
    # evidence -- an `until=progress` timeout with no delta used to return nothing at all.
    progress_delivery: str | None = None
    next_since_updated_at: str | None = None
    suggested_poll_after_s: float | None = None


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
        "attempt": view.attempt,
        "heartbeat_at": view.heartbeat_at,
        "heartbeat_age_seconds": view.heartbeat_age_seconds,
        "evidence_complete": view.evidence_complete,
        "progress_current": view.progress.current,
        "progress_total": view.progress.total,
        "progress_unit": view.progress.unit,
        "progress_message": view.progress.message,
        "workspace_id": view.workspace_id,
        "owner_id": view.owner_id,
        "lease_expires_at": view.lease_expires_at,
        "result_reference": view.result_reference,
        "result_reference_status": view.result_reference_status,
        "receipt_id": view.receipt_id,
        "receipt_status": view.receipt_status,
        "error_code": view.error_code,
        "retryability": view.retryability,
        "terminal": terminal,
        "cancellation_reason": _cancellation_reason(view),
        "poll_after_seconds": _poll_after(view),
        "suggested_poll_after_s": _poll_after(view),
        "eta_seconds": _eta_seconds(view),
        "updated_at": view.updated_at,
        "schema_version": view.schema_version,
        "record_provenance": view.record_provenance,
        "record_consistency": view.record_consistency,
        "record_diagnostics": view.record_diagnostics,
    }


class OperationCoordinator:
    def __init__(
        self,
        *,
        status: OperationStatusReader,
        lister: OperationLister,
        cancel: OperationCancellationRequester,
        failure_evidence: FailureIntelligenceService,
    ) -> None:
        self.status = status
        self.lister = lister
        self.cancel = cancel
        self.failure_evidence = failure_evidence

    @staticmethod
    def _emit_progress(
        reporter: ProgressReporter,
        view: OperationSummary | OperationStatusView,
    ) -> None:
        reporter.report(
            current=view.progress.current,
            total=view.progress.total,
            message=view.progress.message or view.phase,
        )

    def execute(
        self,
        command: OperationCommand,
        *,
        progress_reporter: ProgressReporter | None = None,
    ) -> OperationResult:
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
            if not 1 <= timeout_seconds <= _MAX_WAIT_SECONDS:
                raise _invalid(
                    f"operation wait timeout_seconds must be between 1 and {_MAX_WAIT_SECONDS}"
                )
            if command.until not in _WAIT_UNTIL:
                raise _invalid("operation wait until must be 'progress' or 'terminal'")
            # 'terminal' asks to be woken by the outcome, not by motion: a profile emits a
            # progress delta at every step start and completion, so returning on each one
            # turns "did it pass?" into one round trip per step.
            wake_on_progress = command.until == "progress"
            # An explicit reporter wins (tests, direct callers); otherwise take the one
            # the transport bound for this request. Neither present means poll guidance.
            reporter: ProgressReporter = progress_reporter or current_progress_reporter()
            current = self.status.read(command.operation_id)
            baseline = command.since_updated_at or current.updated_at
            terminal = OperationState(current.state) in TERMINAL_OPERATION_STATES
            changed_since = command.since_updated_at is not None and current.updated_at != baseline
            last_reported_at = ""
            if reporter.enabled:
                self._emit_progress(reporter, current)
                last_reported_at = current.updated_at
            deadline = time.monotonic() + timeout_seconds
            while not terminal and not (changed_since and wake_on_progress):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                time.sleep(min(0.1, remaining))
                current = self.status.read(command.operation_id)
                terminal = OperationState(current.state) in TERMINAL_OPERATION_STATES
                changed_since = current.updated_at != baseline
                # Heartbeat the open request so a progress-capable client learns
                # of forward motion without issuing another poll, and the
                # connector's idle timeout is reset while work continues.
                if reporter.enabled and current.updated_at != last_reported_at:
                    self._emit_progress(reporter, current)
                    last_reported_at = current.updated_at
            # In 'terminal' mode a delta is not an answer, so advancing without finishing
            # is still a timeout -- and the caller needs current evidence to pace the next
            # call, which for a long gate is the ordinary outcome rather than an anomaly.
            timed_out = not terminal if not wake_on_progress else not terminal and not changed_since
            self.status.operations.ctx.record_metric(
                "operation_wait_empty_delta" if timed_out else "operation_wait_delta",
                success=True,
                duration_ms=0.0,
                error_code=None,
            )
            return OperationResult(
                summary=(
                    f"Operation {command.operation_id} reached terminal state"
                    if terminal
                    else (
                        f"Operation {command.operation_id} advanced"
                        if changed_since and wake_on_progress
                        else f"Operation {command.operation_id} wait timed out"
                    )
                ),
                action="wait",
                operation=(
                    operation_evidence(current)
                    if not wake_on_progress or terminal or changed_since
                    else None
                ),
                operations=[],
                cancellation_requested=False,
                truncated=False,
                next_cursor=None,
                changed_since=changed_since,
                timed_out=timed_out,
                # Name the mechanism the caller got rather than making it infer one from
                # whether notifications happened to arrive.
                progress_delivery="pushed" if reporter.enabled else "poll",
                # A terminal wait has nothing to resume; anything else does, and must say
                # so even when it carries no evidence to read the cursor off.
                next_since_updated_at=None if terminal else current.updated_at,
                suggested_poll_after_s=_poll_after(current),
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
