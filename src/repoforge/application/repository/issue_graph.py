from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from ...config import GitHubTicketGraphConfig, RepositoryConfig
from ...domain.errors import ConfigError
from ...domain.tickets import (
    TicketGraph,
    TicketGraphError,
    TicketGraphSnapshot,
    TicketLiveMetadata,
    TicketNode,
    TicketPriority,
    TicketStatus,
    TicketType,
)
from ..context import ApplicationContext
from ..tickets.graph import select_ticket_nodes


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


def _snapshot_payload(snapshot: TicketGraphSnapshot) -> dict[str, Any]:
    return {
        "schema_version": snapshot.graph.schema_version,
        "program_issue": snapshot.graph.program_issue,
        "nodes": [node_payload(node) for node in snapshot.graph.nodes],
        "observed_at": snapshot.observed_at,
        "evidence_complete": snapshot.evidence_complete,
        "unavailable": list(snapshot.unavailable),
        "truncated": snapshot.truncated,
        "live_issues": [
            {
                "number": issue.number,
                "title": issue.title,
                "state": issue.state,
                "body": issue.body,
            }
            for issue in snapshot.live_issues
        ],
    }


def _positive_integer_tuple(value: object) -> tuple[int, ...]:
    if not isinstance(value, list):
        raise ValueError
    result = tuple(value)
    if any(not isinstance(item, int) or isinstance(item, bool) or item <= 0 for item in result):
        raise ValueError
    return tuple(sorted(set(result)))


def _snapshot_from_payload(payload: object) -> TicketGraphSnapshot | None:
    if not isinstance(payload, dict):
        return None
    try:
        raw_nodes = payload["nodes"]
        raw_live = payload["live_issues"]
        if not isinstance(raw_nodes, list) or not isinstance(raw_live, list):
            return None
        nodes = tuple(
            TicketNode(
                number=int(raw["number"]),
                title=str(raw["title"]),
                ticket_type=TicketType(str(raw["type"])),
                priority=TicketPriority(str(raw["priority"])),
                status=TicketStatus(str(raw["status"])),
                parent=int(raw["parent"]) if raw.get("parent") is not None else None,
                blockers=_positive_integer_tuple(raw["blockers"]),
                blocks=_positive_integer_tuple(raw["blocks"]),
                children=_positive_integer_tuple(raw["children"]),
                roadmap=tuple(str(item) for item in raw["roadmap"]),
            )
            for raw in raw_nodes
            if isinstance(raw, dict)
        )
        live = tuple(
            TicketLiveMetadata(
                int(raw["number"]),
                str(raw["title"]),
                str(raw["state"]),
                str(raw["body"]),
            )
            for raw in raw_live
            if isinstance(raw, dict)
        )
        observed_at = payload["observed_at"]
        evidence_complete = payload["evidence_complete"]
        truncated = payload["truncated"]
        if (
            not isinstance(observed_at, str)
            or not isinstance(evidence_complete, bool)
            or not isinstance(truncated, bool)
        ):
            return None
        graph = TicketGraph(int(payload["schema_version"]), int(payload["program_issue"]), nodes)
        return TicketGraphSnapshot(
            graph=graph,
            observed_at=observed_at,
            evidence_complete=evidence_complete,
            unavailable=_positive_integer_tuple(payload["unavailable"]),
            truncated=truncated,
            live_issues=live,
        )
    except (KeyError, TypeError, ValueError):
        return None


def _source(repo: RepositoryConfig, root_issue: int | None) -> GitHubTicketGraphConfig:
    configured = repo.ticket_graph
    if configured is None:
        if root_issue is None:
            raise ConfigError(
                f"Repository {repo.repo_id!r} has no GitHub ticket_graph.root_issue configured"
            )
        return GitHubTicketGraphConfig(root_issue=root_issue)
    if root_issue is None or root_issue == configured.root_issue:
        return configured
    if not isinstance(root_issue, int) or isinstance(root_issue, bool) or root_issue <= 0:
        raise TicketGraphError("root_issue must be a positive issue number")
    return replace(configured, root_issue=root_issue)


def read_github_ticket_snapshot(
    ctx: ApplicationContext,
    repo: RepositoryConfig,
    *,
    root_issue: int | None,
    fresh: bool,
) -> tuple[TicketGraphSnapshot, bool]:
    source = _source(repo, root_issue)
    if ctx.ticket_graphs is None:
        raise ConfigError("GitHub ticket graph adapter is unavailable")
    cache = ctx.github_read_cache
    now_epoch = ctx.now_epoch()
    if not fresh and cache is not None:
        cached = cache.get(
            repo.repo_id,
            repo.path,
            "graph",
            source.root_issue,
            ttl_seconds=ctx.config.server.github_read_cache_ttl_seconds,
            now_epoch=now_epoch,
        )
        snapshot = _snapshot_from_payload(cached)
        if snapshot is not None:
            return snapshot, True
    snapshot = ctx.ticket_graphs.read(repo.path, source, max_items=200)
    if cache is not None:
        cache.put(
            repo.repo_id,
            repo.path,
            "graph",
            source.root_issue,
            _snapshot_payload(snapshot),
            now_epoch=now_epoch,
        )
    return snapshot, False


@dataclass(frozen=True, slots=True)
class RepositoryIssueGraphCommand:
    repo_id: str
    root_issue: int | None = None
    status: str | None = None
    priority: str | None = None
    initiative: int | None = None
    fresh: bool = False


@dataclass(frozen=True, slots=True)
class RepositoryIssueGraphResult:
    repo_id: str
    source: str
    cache_hit: bool
    program_issue: int | None
    observed_at: str
    evidence_complete: bool
    unavailable: list[int]
    nodes: list[dict[str, Any]]
    node_count: int
    truncated: bool


class RepositoryIssueGraphReader:
    """Bounded, read-only query over one repository's GitHub-native ticket graph."""

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
            "fresh": c.fresh,
        }

        def op() -> RepositoryIssueGraphResult:
            if repo.ticket_graph is None and c.root_issue is None:
                details.update(
                    source="github",
                    cache_hit=False,
                    node_count=0,
                    truncated=False,
                    evidence_complete=False,
                )
                return RepositoryIssueGraphResult(
                    c.repo_id,
                    "github",
                    False,
                    None,
                    self.ctx.clock.now_iso(),
                    False,
                    [],
                    [],
                    0,
                    False,
                )
            snapshot, cache_hit = read_github_ticket_snapshot(
                self.ctx,
                repo,
                root_issue=c.root_issue,
                fresh=c.fresh,
            )
            status = _parse_status(c.status)
            priority = _parse_priority(c.priority)
            nodes, selection_truncated = select_ticket_nodes(
                snapshot.graph,
                root_issue=c.root_issue,
                status=status,
                priority=priority,
                initiative=c.initiative,
            )
            truncated = snapshot.truncated or selection_truncated
            details["source"] = "github"
            details["cache_hit"] = cache_hit
            details["node_count"] = len(nodes)
            details["truncated"] = truncated
            details["evidence_complete"] = snapshot.evidence_complete
            return RepositoryIssueGraphResult(
                c.repo_id,
                "github",
                cache_hit,
                snapshot.graph.program_issue,
                snapshot.observed_at,
                snapshot.evidence_complete,
                list(snapshot.unavailable),
                [node_payload(node) for node in nodes],
                len(nodes),
                truncated,
            )

        return self.ctx.audited("repo_issue_graph", details, op)
