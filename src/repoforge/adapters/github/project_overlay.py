"""Project V2 overlay read for one bounded ticket-graph observation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

from ...config import GitHubTicketGraphConfig, ServerConfig
from ...domain.errors import CommandError
from ...ports.command import CommandExecutor, CommandResult


def _run(
    executor: CommandExecutor, server: ServerConfig, argv: list[str], *, cwd: Path
) -> CommandResult:
    return executor.run(
        argv,
        cwd=cwd,
        timeout=server.default_command_timeout_seconds,
        output_limit=min(max(server.max_tool_output_chars, 500_000), 5_000_000),
    )


def _object(result: CommandResult, context: str) -> dict[str, Any]:
    if result.stdout_truncated:
        raise CommandError(f"{context} returned oversized JSON")
    try:
        payload: Any = json.loads(result.stdout or "null")
    except json.JSONDecodeError as exc:
        raise CommandError(f"{context} returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise CommandError(f"{context} returned a non-object JSON value")
    return cast(dict[str, Any], payload)


def read_project_values(
    executor: CommandExecutor,
    server: ServerConfig,
    cwd: Path,
    slug: str,
    source: GitHubTicketGraphConfig,
    wanted: set[int],
) -> tuple[dict[int, dict[str, str]], bool, int]:
    """Read Project V2 field values for the wanted issue numbers.

    Returns ``(values_by_number, complete, stdout_bytes)``. ``complete`` is
    false only when the item listing itself was truncated; the caller
    isolates any read failure as an unavailable capability.
    """
    if source.project_owner is None or source.project_number is None:
        return {}, True, 0
    result = _run(
        executor,
        server,
        [
            "gh",
            "project",
            "item-list",
            str(source.project_number),
            "--owner",
            source.project_owner,
            "--format",
            "json",
            "--limit",
            str(min(max(len(wanted) + 100, 100), 1000)),
        ],
        cwd=cwd,
    )
    payload = _object(result, "gh project item-list")
    raw_items = payload.get("items")
    if not isinstance(raw_items, list):
        raise CommandError("gh project item-list returned no items list")
    result_values: dict[int, dict[str, str]] = {}
    field_names = (
        source.status_field,
        source.priority_field,
        source.initiative_field,
        source.type_field,
    )
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        content = raw.get("content")
        if not isinstance(content, dict):
            continue
        number = content.get("number")
        if not isinstance(number, int) or isinstance(number, bool):
            continue
        repository = content.get("repository")
        if isinstance(repository, dict):
            repository = repository.get("nameWithOwner")
        if number not in wanted or repository != slug:
            continue
        values: dict[str, str] = {}
        for field_name in field_names:
            value = raw.get(field_name)
            if value is not None:
                values[field_name] = str(value)
        result_values[int(number)] = values
    return (
        result_values,
        len(raw_items) < min(max(len(wanted) + 100, 100), 1000),
        len(result.stdout.encode("utf-8")),
    )
