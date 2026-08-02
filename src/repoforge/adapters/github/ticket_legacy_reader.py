"""Legacy one-process-per-issue GitHub issue metadata reader.

Retained for fixture and API compatibility — the ``GitHubTicketGraphReader``
is not used in the ticket-graph read path (``CommandGitHubTicketGraphGateway``
does the work with batched GraphQL instead).  New code should prefer the
batched reader in ``ticket_graph.py``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ...domain.errors import CommandError
from ...domain.tickets import TicketGraphError, TicketLiveMetadata
from ...ports.command import CommandExecutor

_MAX_NODES = 200
_MAX_BODY_CHARS = 200_000
_MAX_COMMENTS = 20
_MAX_COMMENT_CHARS = 20_000

#: Repository identity validation pattern shared with graph_decode.
_REPOSITORY_RE = r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$"


def _repository_fullmatch(slug: str) -> bool:
    import re

    return bool(re.match(_REPOSITORY_RE, slug))


class GitHubTicketGraphReader:
    """Legacy bounded metadata reader retained for fixture and API compatibility.

    Reads one issue at a time via ``gh issue view`` — not the batched GraphQL
    path used by ``CommandGitHubTicketGraphGateway``.
    """

    def __init__(self, executor: CommandExecutor, *, cwd: Path) -> None:
        self._executor = executor
        self._cwd = cwd

    def read(
        self, repository: str, issue_numbers: tuple[int, ...]
    ) -> tuple[TicketLiveMetadata, ...]:
        if not _repository_fullmatch(repository):
            raise TicketGraphError("live repository must use owner/name format")
        if not issue_numbers or len(issue_numbers) > 100:
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
                        "number,title,state,body,comments",
                    ),
                    cwd=self._cwd,
                    timeout=30,
                    output_limit=_MAX_BODY_CHARS + 10_000,
                )
            except CommandError:
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
            raw_comments = payload.get("comments")
            comments: list[str] = []
            if isinstance(raw_comments, list):
                for raw_comment in raw_comments[:_MAX_COMMENTS]:
                    comment_body = (
                        raw_comment.get("body") if isinstance(raw_comment, dict) else None
                    )
                    if isinstance(comment_body, str) and len(comment_body) <= _MAX_COMMENT_CHARS:
                        comments.append(comment_body)
            if (
                live_number != issue_number
                or not isinstance(title, str)
                or not title.strip()
                or state not in {"OPEN", "CLOSED"}
                or not isinstance(body, str)
                or len(body) > _MAX_BODY_CHARS
            ):
                continue
            snapshots.append(
                TicketLiveMetadata(issue_number, title.strip(), state, body, tuple(comments))
            )
        return tuple(snapshots)


__all__ = ["GitHubTicketGraphReader"]
