from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ...domain.tickets import TicketGraphError, TicketNode, TicketPriority, TicketStatus
from ..context import ApplicationContext
from ..tickets.graph import select_ticket_nodes
from ..tickets.repo_manifest import load_repo_ticket_graph


def node_payload(node: TicketNode) -> dict[str, Any]:
    return {
        "number": node.number,
        "title": node.title,
        "type": node.ticket_type.value,
        "priority": node.priority.value,
        "status": node.status.value,
        "parent": node.parent,
        "blockers": list(node.blockers),
        "blocks": list(node.blocks),
        "children": list(node.children),
        "roadmap": list(node.roadmap),
    }


def _parse_status(value: str | None) -> TicketStatus | None:
    if value is None:
        return None
    try:
        return TicketStatus(value)
    except ValueError as exc:
        allowed = ", ".join(item.value for item in TicketStatus)
        raise TicketGraphError(f"status must be one of: {allowed}") from exc


def _parse_priority(value: str | None) -> TicketPriority | None:
    if value is None:
        return None
    try:
        return TicketPriority(value)
    except ValueError as exc:
        allowed = ", ".join(item.value for item in TicketPriority)
        raise TicketGraphError(f"priority must be one of: {allowed}") from exc


@dataclass(frozen=True, slots=True)
class RepositoryIssueGraphCommand:
    repo_id: str
    root_issue: int | None = None
    status: str | None = None
    priority: str | None = None
    initiative: int | None = None


@dataclass(frozen=True, slots=True)
class RepositoryIssueGraphResult:
    repo_id: str
    manifest_found: bool
    program_issue: int | None
    nodes: list[dict[str, Any]]
    node_count: int
    truncated: bool


class RepositoryIssueGraphReader:
    """Bounded, read-only query over one repository's checked-in ticket graph."""

    def __init__(self, ctx: ApplicationContext):
        self.ctx = ctx

    def execute(self, c: RepositoryIssueGraphCommand) -> RepositoryIssueGraphResult:
        repo = self.ctx.repo(c.repo_id)
        details: dict[str, object] = {
            "repo_id": c.repo_id,
            "root_issue": c.root_issue,
            "status": c.status,
            "priority": c.priority,
            "initiative": c.initiative,
        }

        def op() -> RepositoryIssueGraphResult:
            graph = load_repo_ticket_graph(repo.path)
            if graph is None:
                details["manifest_found"] = False
                details["node_count"] = 0
                return RepositoryIssueGraphResult(c.repo_id, False, None, [], 0, False)
            status = _parse_status(c.status)
            priority = _parse_priority(c.priority)
            nodes, truncated = select_ticket_nodes(
                graph,
                root_issue=c.root_issue,
                status=status,
                priority=priority,
                initiative=c.initiative,
            )
            details["manifest_found"] = True
            details["node_count"] = len(nodes)
            details["truncated"] = truncated
            return RepositoryIssueGraphResult(
                c.repo_id,
                True,
                graph.program_issue,
                [node_payload(node) for node in nodes],
                len(nodes),
                truncated,
            )

        return self.ctx.audited("repo_issue_graph", details, op)
