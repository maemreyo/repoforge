"""Shared durable-operation wait/failure-projection helpers.

Extracted from `WorkspaceVerifier` (application/workspace/verify.py), which used these as
private instance methods shared across its diagnostic/profile/adhoc modes. `workspace_exec`
(#376) needs the identical wait-then-raise-or-return semantics for its own durable adhoc
admission, so this makes the logic standalone instead of duplicating it a second time.
"""

from __future__ import annotations

import time
from typing import Any

from ...domain.errors import ErrorCode, RepoForgeError
from ...domain.operation_task import (
    TERMINAL_OPERATION_STATES,
    OperationRetryability,
    OperationState,
    OperationTask,
)
from ..context import ApplicationContext
from .manager import OperationManager

FOREGROUND_WAIT_SECONDS = 25.0
FOREGROUND_POLL_SECONDS = 0.1


def wait_for_operation(
    ctx: ApplicationContext, operations: OperationManager, operation_id: str
) -> tuple[OperationTask, dict[str, Any] | None]:
    deadline = time.monotonic() + FOREGROUND_WAIT_SECONDS
    task = operations.status(operation_id)
    while task.state not in TERMINAL_OPERATION_STATES and time.monotonic() < deadline:
        time.sleep(FOREGROUND_POLL_SECONDS)
        task = operations.status(operation_id)
    result = None
    if task.state is OperationState.SUCCEEDED and ctx.operation_result_store is not None:
        result = ctx.operation_result_store.read(operation_id)
    raise_terminal_failure(task)
    return task, result


def raise_terminal_failure(task: OperationTask) -> None:
    """Surface a durable execution failure to the caller that waited for it.

    Making execution durable moved the command off the request thread; it did not turn a
    refusal into a verdict. A caller that waited for this operation would otherwise receive
    `outcome="failed"` with the exact reason -- the typed code and message -- reachable only
    through a second call, so the terminal failure is re-raised here with its evidence intact.
    """
    if task.state not in {
        OperationState.FAILED,
        OperationState.ORPHANED,
        OperationState.CANCELLED,
    }:
        return
    try:
        code = ErrorCode(str(task.error_code))
    except ValueError:
        code = (
            ErrorCode.COMMAND_FAILED
            if task.state is OperationState.CANCELLED
            else ErrorCode.INTERNAL_ERROR
        )
    message = task.error_message or (
        f"Durable verification operation {task.operation_id} {task.state.value}"
    )
    raise RepoForgeError(
        message,
        code=code,
        retryable=task.retryability is OperationRetryability.AUTOMATIC,
        safe_next_action=(f"Read operation {task.operation_id} for the full durable evidence."),
        details={
            "operation_id": task.operation_id,
            "operation_state": task.state.value,
            "operation_phase": task.phase,
            "attempt": task.attempt,
        },
    )


def operation_projection(task: OperationTask) -> dict[str, object]:
    return {
        "operation_id": task.operation_id,
        "kind": task.kind,
        "state": task.state.value,
        "phase": task.phase,
        "progress_current": task.progress_current,
        "progress_total": task.progress_total,
        "cancellation_reason": ("cancelled" if task.state is OperationState.CANCELLED else None),
        "poll_after_seconds": (None if task.state in TERMINAL_OPERATION_STATES else 1.0),
    }
