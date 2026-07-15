from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..context import ApplicationContext
from ..tickets.graph import select_ready_tickets, validate_ticket_graph
from ..tickets.repo_manifest import load_repo_ticket_graph
from .issue_graph import node_payload


@dataclass(frozen=True, slots=True)
class RepositoryIssueNextCommand:
    repo_id: str
    root_issue: int | None = None
    limit: int = 1


@dataclass(frozen=True, slots=True)
class RepositoryIssueNextResult:
    repo_id: str
    manifest_found: bool
    valid: bool
    diagnostics: list[dict[str, Any]]
    tickets: list[dict[str, Any]]


class RepositoryIssueNextReader:
    """Select the next executable implementation ticket from the checked-in graph.

    Read-only and closed-world: it never assigns, edits, closes, or reorders
    an issue. A stale or invalid manifest is surfaced as diagnostics rather
    than silently returning an empty ready queue.
    """

    def __init__(self, ctx: ApplicationContext):
        self.ctx = ctx

    def execute(self, c: RepositoryIssueNextCommand) -> RepositoryIssueNextResult:
        repo = self.ctx.repo(c.repo_id)
        details: dict[str, object] = {
            "repo_id": c.repo_id,
            "root_issue": c.root_issue,
            "limit": c.limit,
        }

        def op() -> RepositoryIssueNextResult:
            graph = load_repo_ticket_graph(repo.path)
            if graph is None:
                details["manifest_found"] = False
                details["valid"] = False
                details["ticket_count"] = 0
                return RepositoryIssueNextResult(c.repo_id, False, False, [], [])
            diagnostics = validate_ticket_graph(graph)
            if diagnostics:
                details["manifest_found"] = True
                details["valid"] = False
                details["diagnostic_count"] = len(diagnostics)
                details["ticket_count"] = 0
                return RepositoryIssueNextResult(
                    c.repo_id,
                    True,
                    False,
                    [
                        {
                            "code": item.code,
                            "issue_number": item.issue_number,
                            "message": item.message,
                        }
                        for item in diagnostics
                    ],
                    [],
                )
            tickets = select_ready_tickets(graph, limit=c.limit, root_issue=c.root_issue)
            details["manifest_found"] = True
            details["valid"] = True
            details["ticket_count"] = len(tickets)
            return RepositoryIssueNextResult(
                c.repo_id, True, True, [], [node_payload(item) for item in tickets]
            )

        return self.ctx.audited("repo_issue_next", details, op)
