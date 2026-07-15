"""Bounded read-only GitHub issue metadata reads for ticket-graph drift checks."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from ...domain.errors import CommandError
from ...domain.tickets import TicketGraphError, TicketLiveMetadata
from ...ports.command import CommandExecutor

_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_MAX_ISSUES = 100
_MAX_BODY_CHARS = 100_000


class GitHubTicketGraphReader:
    """Read normalized issue snapshots without invoking any GitHub write command."""

    def __init__(self, executor: CommandExecutor, *, cwd: Path) -> None:
        self._executor = executor
        self._cwd = cwd

    def read(
        self, repository: str, issue_numbers: tuple[int, ...]
    ) -> tuple[TicketLiveMetadata, ...]:
        if _REPOSITORY.fullmatch(repository) is None:
            raise TicketGraphError("live repository must use owner/name format")
        if not issue_numbers or len(issue_numbers) > _MAX_ISSUES:
            raise TicketGraphError("live issue read must contain between 1 and 100 issues")
        if tuple(sorted(set(issue_numbers))) != issue_numbers:
            raise TicketGraphError("live issue numbers must be sorted and unique")

        snapshots: list[TicketLiveMetadata] = []
        for issue_number in issue_numbers:
            if (
                not isinstance(issue_number, int)
                or isinstance(issue_number, bool)
                or issue_number <= 0
            ):
                raise TicketGraphError("live issue numbers must be positive integers")
            try:
                result = self._executor.run(
                    (
                        "gh",
                        "issue",
                        "view",
                        str(issue_number),
                        "--repo",
                        repository,
                        "--json",
                        "number,title,state,body",
                    ),
                    cwd=self._cwd,
                    timeout=30,
                    output_limit=_MAX_BODY_CHARS + 10_000,
                )
            except CommandError:
                # Unreadable for this one issue (missing, permission denied, gh
                # outage). The caller sees it as an issue absent from the live
                # snapshot rather than aborting every other bounded read.
                continue
            try:
                payload: Any = json.loads(result.stdout)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            live_number = payload.get("number")
            title = payload.get("title")
            state = payload.get("state")
            body = payload.get("body")
            if (
                live_number != issue_number
                or not isinstance(title, str)
                or not title.strip()
                or not isinstance(state, str)
                # `gh issue view` also resolves pull request numbers (GitHub
                # shares one number sequence between issues and PRs); a state
                # outside {OPEN, CLOSED} means this number is not an issue.
                or state not in {"OPEN", "CLOSED"}
                or not isinstance(body, str)
                or len(body) > _MAX_BODY_CHARS
            ):
                continue
            snapshots.append(TicketLiveMetadata(issue_number, title.strip(), state, body))
        return tuple(snapshots)
