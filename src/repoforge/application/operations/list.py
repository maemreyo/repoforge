"""Bounded deterministic listing for durable operations."""

from __future__ import annotations

import re
from dataclasses import dataclass

from ...domain.errors import ErrorCode, RepoForgeError
from ...domain.operation_task import OperationState
from .dto import OperationSummary, operation_summary
from .manager import OperationManager

_SCOPE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


@dataclass(frozen=True, slots=True)
class OperationListCommand:
    scope: str | None = None
    state: str | None = None
    limit: int = 50
    cursor: str | None = None


@dataclass(frozen=True, slots=True)
class OperationListResult:
    operations: list[OperationSummary]
    next_cursor: str | None
    scan_truncated: bool
    scope: str | None
    state: str | None
    limit: int


def _invalid(message: str) -> RepoForgeError:
    return RepoForgeError(
        message,
        code=ErrorCode.OPERATION_INVALID,
        safe_next_action="Use operation_list with a returned cursor and a supported task/workspace scope.",
    )


def _parse_scope(scope: str | None) -> tuple[str, str] | None:
    if scope is None:
        return None
    prefix, separator, value = scope.partition(":")
    if separator != ":" or prefix not in {"task", "workspace"} or not value:
        raise _invalid("Operation scope must be task:<id> or workspace:<id>")
    if _SCOPE_ID.fullmatch(value) is None:
        raise _invalid("Operation scope identifier is invalid")
    return prefix, value


class OperationLister:
    def __init__(self, operations: OperationManager):
        self.operations = operations

    def execute(self, command: OperationListCommand) -> OperationListResult:
        if not isinstance(command.limit, int) or isinstance(command.limit, bool):
            raise _invalid("Operation list limit must be an integer")
        limit = max(1, min(command.limit, 100))
        scope = _parse_scope(command.scope)
        try:
            selected_state = OperationState(command.state) if command.state is not None else None
        except ValueError as exc:
            raise _invalid(f"Unsupported operation state: {command.state}") from exc

        def read() -> OperationListResult:
            page = self.operations.list_records(max_records=2_000)
            records = list(page.records)
            if scope is not None:
                kind, identity = scope
                records = [
                    task
                    for task in records
                    if (task.task_id if kind == "task" else task.workspace_id) == identity
                ]
            if selected_state is not None:
                records = [task for task in records if task.state is selected_state]

            start = 0
            if command.cursor is not None:
                matches = [
                    index
                    for index, task in enumerate(records)
                    if task.operation_id == command.cursor
                ]
                if not matches:
                    raise _invalid("Operation cursor is unknown or stale for the selected filters")
                start = matches[0] + 1
            selected = records[start : start + limit]
            has_more = start + len(selected) < len(records)
            next_cursor = selected[-1].operation_id if selected and has_more else None
            return OperationListResult(
                operations=[operation_summary(task) for task in selected],
                next_cursor=next_cursor,
                scan_truncated=page.scan_truncated,
                scope=command.scope,
                state=selected_state.value if selected_state is not None else None,
                limit=limit,
            )

        return self.operations.ctx.audited(
            "operation_list",
            {
                "scope": command.scope,
                "state": command.state,
                "limit": limit,
                "cursor": command.cursor,
            },
            read,
        )
