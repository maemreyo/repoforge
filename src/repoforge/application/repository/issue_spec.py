from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from ...domain.tickets import TicketGraph, TicketLiveMetadata
from ..context import ApplicationContext
from ..tickets.graph import compare_live_ticket_metadata
from ..tickets.repo_manifest import load_repo_ticket_graph
from .issue_graph import node_payload

_HEADING = re.compile(r"(?m)^#{2,3}\s+(.+)$")


def _first_heading(body: str) -> str | None:
    match = _HEADING.search(body)
    return match.group(1).strip() if match is not None else None


@dataclass(frozen=True, slots=True)
class RepositoryIssueSpecCommand:
    repo_id: str
    issue_number: int


@dataclass(frozen=True, slots=True)
class RepositoryIssueSpecResult:
    repo_id: str
    issue_number: int
    manifest_found: bool
    node: dict[str, Any] | None
    live: dict[str, Any]
    drift: list[dict[str, Any]]
    comments: list[dict[str, Any]]


class RepositoryIssueSpecReader:
    """Bounded reference bundle for one ticket: manifest node, live issue,
    drift against the manifest, and comment references (each tagged with
    its first Markdown heading, if any) so an agent can locate the
    specification comment without reconstructing prior chat.
    """

    def __init__(self, ctx: ApplicationContext):
        self.ctx = ctx

    def execute(self, c: RepositoryIssueSpecCommand) -> RepositoryIssueSpecResult:
        if c.issue_number <= 0:
            raise ValueError("issue_number must be positive")
        repo = self.ctx.repo(c.repo_id)
        graph = load_repo_ticket_graph(repo.path)
        node = None
        if graph is not None:
            node = next((item for item in graph.nodes if item.number == c.issue_number), None)

        live_payload = self.ctx.audited(
            "repo_issue_spec",
            {"repo_id": c.repo_id, "issue_number": c.issue_number},
            lambda: self.ctx.github.issue_read(repo.path, c.issue_number),
        )

        comments: list[dict[str, Any]] = []
        raw_comments = live_payload.get("comments")
        if isinstance(raw_comments, list):
            for item in raw_comments:
                if not isinstance(item, dict):
                    continue
                body = item.get("body")
                comments.append(
                    dict(item, heading=_first_heading(body) if isinstance(body, str) else None)
                )

        drift: list[dict[str, Any]] = []
        if node is not None and graph is not None:
            live_metadata = TicketLiveMetadata(
                c.issue_number,
                str(live_payload.get("title") or ""),
                str(live_payload.get("state") or ""),
                str(live_payload.get("body") or ""),
            )
            single_node_graph = TicketGraph(graph.schema_version, graph.program_issue, (node,))
            drift = [
                {"code": item.code, "message": item.message}
                for item in compare_live_ticket_metadata(single_node_graph, (live_metadata,))
            ]

        return RepositoryIssueSpecResult(
            c.repo_id,
            c.issue_number,
            graph is not None,
            node_payload(node) if node is not None else None,
            live_payload,
            drift,
            comments,
        )
