from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any

from ...config import GitHubTicketGraphConfig, RepositoryConfig
from ...domain.errors import ConfigError, ErrorCode, RepoForgeError
from ...domain.tickets import (
    GITHUB_API_VERSION,
    TICKET_GRAPH_READER_VERSION,
    CapabilityCoverage,
    GraphEvidenceCapability,
    TicketDiagnostic,
    TicketGraph,
    TicketGraphError,
    TicketGraphReadStats,
    TicketGraphSnapshot,
    TicketLiveMetadata,
    TicketNode,
    TicketPriority,
    TicketStatus,
    TicketType,
    github_slug_from_remote_url,
)
from ..context import ApplicationContext
from ..tickets.graph import select_ticket_nodes
from ..tickets.live import ticket_delivery_payload, ticket_live_state_from_issue

_DIAGNOSTICS_LIMIT = 100


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


def _capability_payload(coverage: CapabilityCoverage) -> dict[str, Any]:
    return {
        "capability": coverage.capability.value,
        "complete": coverage.complete,
        "unavailable": list(coverage.unavailable),
        "truncated": coverage.truncated,
    }


def capability_coverage_payload(snapshot: TicketGraphSnapshot | None) -> list[dict[str, Any]]:
    """Per-capability completeness (issue, comments, sub_issues, dependencies,
    project_overlay) so a caller can tell exactly which GitHub read is missing
    instead of one blanket `evidence_complete` flag."""
    if snapshot is None:
        return []
    return [_capability_payload(item) for item in snapshot.capability_coverage]


def is_capability_complete_for_issue(
    snapshot: TicketGraphSnapshot | None,
    capability: GraphEvidenceCapability,
    issue_number: int,
) -> bool:
    """Whether one specific capability's evidence is trustworthy for one issue.

    A capability with no coverage entry (older cached snapshot, or a snapshot
    that never observed this issue) is treated as complete: absence of
    evidence about a capability is not evidence the capability failed.
    """
    if snapshot is None:
        return False
    for coverage in snapshot.capability_coverage:
        if coverage.capability is capability:
            return issue_number not in coverage.unavailable
    return True


def _coverage(
    configured_root: int | None,
    snapshot: TicketGraphSnapshot | None = None,
    *,
    diagnostics_count: int = 0,
    diagnostics_truncated: bool = False,
) -> dict[str, Any]:
    return {
        "configured_root": configured_root,
        "observed_root": snapshot.graph.program_issue if snapshot is not None else None,
        "observed_nodes": len(snapshot.graph.nodes) if snapshot is not None else 0,
        "unavailable": list(snapshot.unavailable) if snapshot is not None else [],
        "truncated": snapshot.truncated if snapshot is not None else False,
        "evidence_complete": snapshot.evidence_complete if snapshot is not None else False,
        "capabilities": capability_coverage_payload(snapshot),
        "diagnostics_count": diagnostics_count,
        "diagnostics_truncated": diagnostics_truncated,
    }


def _incomplete_graph_diagnostic(snapshot: TicketGraphSnapshot) -> dict[str, Any]:
    reasons: list[str] = []
    incomplete_capabilities = [
        item.capability.value for item in snapshot.capability_coverage if not item.complete
    ]
    if incomplete_capabilities:
        reasons.append("incomplete capabilities: " + ", ".join(incomplete_capabilities))
    if snapshot.unavailable:
        reasons.append(
            "unavailable issues: " + ", ".join(str(item) for item in snapshot.unavailable)
        )
    if snapshot.truncated:
        reasons.append("the bounded traversal was truncated")
    if not reasons:
        reasons.append("one or more GitHub relationships could not be verified")
    return {
        "code": "GRAPH_EVIDENCE_INCOMPLETE",
        "issue_number": snapshot.graph.program_issue,
        "message": "GitHub ticket graph evidence is incomplete; " + "; ".join(reasons),
    }


def _evolution_by_number(snapshot: TicketGraphSnapshot) -> dict[int, dict[str, object]]:
    return {
        issue.number: ticket_delivery_payload(
            ticket_live_state_from_issue(
                {
                    "number": issue.number,
                    "state": issue.state,
                    "body": issue.body,
                    "comments": [{"body": body} for body in issue.comments],
                },
                expected_number=issue.number,
            ).delivery
        )
        for issue in snapshot.live_issues
    }


def _read_stats_payload(
    stats: TicketGraphReadStats | None,
    *,
    cache_miss_reason: str | None = None,
    cache_age_ms: float | None = None,
) -> dict[str, Any] | None:
    if stats is None:
        return None
    payload: dict[str, Any] = {
        "source": stats.source,
        "provider_processes": stats.provider_processes,
        "captured_stdout_bytes": stats.captured_stdout_bytes,
        "provider_process_duration_ms": stats.provider_process_duration_ms,
        "per_capability": [
            {
                "capability": item.capability.value,
                "provider_processes": item.provider_processes,
                "captured_stdout_bytes": item.captured_stdout_bytes,
                "provider_process_duration_ms": item.provider_process_duration_ms,
            }
            for item in stats.per_capability
        ],
    }
    if cache_miss_reason is not None:
        payload["cache_miss_reason"] = cache_miss_reason
    if cache_age_ms is not None:
        payload["cache_age_ms"] = cache_age_ms
    return payload


def _cache_hit_read_stats(*, cache_hit_reason: str, cache_age_ms: float | None) -> dict[str, Any]:
    """Zeroed provider traffic for a TTL cache hit (no provider calls made)."""
    payload: dict[str, Any] = {
        "source": "cache",
        "provider_processes": 0,
        "captured_stdout_bytes": 0,
        "provider_process_duration_ms": 0.0,
        "per_capability": [],
        "cache_hit_reason": cache_hit_reason,
    }
    if cache_age_ms is not None:
        payload["cache_age_ms"] = cache_age_ms
    return payload


def _source_digest(source: GitHubTicketGraphConfig) -> str:
    """Digest of the reviewed source configuration the snapshot was read under.

    Binds the cache entry to the repository/project fields and field names the
    reader used, so a configuration change is a cache miss instead of stale
    evidence served as current.
    """
    canonical = json.dumps(
        {
            "repository": source.repository,
            "root_issue": source.root_issue,
            "project_owner": source.project_owner,
            "project_number": source.project_number,
            "status_field": source.status_field,
            "priority_field": source.priority_field,
            "initiative_field": source.initiative_field,
            "type_field": source.type_field,
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _snapshot_payload(
    snapshot: TicketGraphSnapshot, source: GitHubTicketGraphConfig
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": snapshot.graph.schema_version,
        "program_issue": snapshot.graph.program_issue,
        "nodes": [node_payload(node) for node in snapshot.graph.nodes],
        "observed_at": snapshot.observed_at,
        "evidence_complete": snapshot.evidence_complete,
        "unavailable": list(snapshot.unavailable),
        "truncated": snapshot.truncated,
        "capability_coverage": capability_coverage_payload(snapshot),
        "live_issues": [
            {
                "number": issue.number,
                "title": issue.title,
                "state": issue.state,
                "body": issue.body,
                "comments": list(issue.comments),
            }
            for issue in snapshot.live_issues
        ],
        "diagnostics": [
            {
                "code": diagnostic.code,
                "issue_number": diagnostic.issue_number,
                "message": diagnostic.message,
            }
            for diagnostic in snapshot.diagnostics
        ],
    }
    payload["bindings"] = {
        "cache_schema_version": 2,
        "reader_contract_version": TICKET_GRAPH_READER_VERSION,
        "api_version": GITHUB_API_VERSION,
        "repository_slug": snapshot.repository_slug,
        "source_digest": _source_digest(source),
        "payload_checksum": hashlib.sha256(
            json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest(),
    }
    return payload


def _payload_bindings_valid(
    payload: object, source: GitHubTicketGraphConfig, expected_slug: str | None
) -> bool:
    """Whether a cached payload's bindings match the current reader and config.

    Bindings pin the resolved repository slug, the reviewed source
    configuration digest, the reader/query contract version, and the API
    version, and the checksum is recomputed over the payload body (excluding
    bindings) so a corrupted or hand-edited cache entry fails closed instead of
    being served. An unverifiable expected slug (no configured repository and
    no parseable github.com remote) fails closed to a miss.
    """
    if not isinstance(payload, dict):
        return False
    bindings = payload.get("bindings")
    if not isinstance(bindings, dict):
        return False
    if bindings.get("cache_schema_version") != 2:
        return False
    if bindings.get("reader_contract_version") != TICKET_GRAPH_READER_VERSION:
        return False
    if bindings.get("api_version") != GITHUB_API_VERSION:
        return False
    if expected_slug is None or bindings.get("repository_slug") != expected_slug:
        return False
    if bindings.get("source_digest") != _source_digest(source):
        return False
    stored = bindings.get("payload_checksum")
    if not isinstance(stored, str) or len(stored) != 64:
        return False
    body = {key: value for key, value in payload.items() if key != "bindings"}
    computed = hashlib.sha256(
        json.dumps(body, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    return hmac.compare_digest(computed, stored)


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
                tuple(str(item) for item in raw.get("comments", [])),
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
        raw_coverage = payload.get("capability_coverage", [])
        if not isinstance(raw_coverage, list):
            return None
        capability_coverage = tuple(
            CapabilityCoverage(
                capability=GraphEvidenceCapability(str(item["capability"])),
                complete=bool(item["complete"]),
                unavailable=_positive_integer_tuple(item["unavailable"]),
                truncated=bool(item["truncated"]),
            )
            for item in raw_coverage
            if isinstance(item, dict)
        )
        graph = TicketGraph(int(payload["schema_version"]), int(payload["program_issue"]), nodes)
        diagnostics: list[TicketDiagnostic] = []
        for item in payload.get("diagnostics", []):
            if not isinstance(item, dict) or not isinstance(item.get("issue_number"), int):
                continue
            diagnostics.append(
                TicketDiagnostic(
                    code=str(item.get("code", "")),
                    issue_number=int(item["issue_number"]),
                    message=str(item.get("message", "")),
                )
            )
        bindings = payload.get("bindings")
        repository_slug = (
            bindings.get("repository_slug")
            if isinstance(bindings, dict) and isinstance(bindings.get("repository_slug"), str)
            else None
        )
        return TicketGraphSnapshot(
            graph=graph,
            observed_at=observed_at,
            evidence_complete=evidence_complete,
            unavailable=_positive_integer_tuple(payload["unavailable"]),
            truncated=truncated,
            live_issues=live,
            capability_coverage=capability_coverage,
            diagnostics=tuple(diagnostics),
            repository_slug=repository_slug,
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


def _expected_slug(
    ctx: ApplicationContext, repo: RepositoryConfig, source: GitHubTicketGraphConfig
) -> str | None:
    """The repository slug the reader should be observing, without provider calls.

    A configured ``ticket_graph.repository`` is authoritative; otherwise the
    configured remote's URL is parsed locally so a remote repointing is a cache
    miss. An unresolvable identity returns ``None`` and callers fail closed.
    """
    if source.repository is not None:
        return source.repository
    try:
        result = ctx.git.remote_url(repo.path, repo.remote)
    except RepoForgeError:
        return None
    if result.returncode != 0:
        return None
    return github_slug_from_remote_url(result.stdout.strip())


def _observed_age_ms(now_epoch: float, observed_at: str) -> float | None:
    try:
        observed = datetime.fromisoformat(observed_at)
    except ValueError:
        return None
    if observed.tzinfo is None:
        return None
    return round((now_epoch - observed.timestamp()) * 1000, 3)


def read_github_ticket_snapshot(
    ctx: ApplicationContext,
    repo: RepositoryConfig,
    *,
    root_issue: int | None,
    fresh: bool,
) -> tuple[TicketGraphSnapshot, bool, dict[str, object]]:
    """Read (or reuse) one bounded graph snapshot for the repository.

    Returns ``(snapshot, cache_hit, cache_context)`` where ``cache_context``
    reports why the cache was hit or missed and the cached snapshot's age, so
    telemetry never has to guess whether a result was fresh provider evidence.
    """
    source = _source(repo, root_issue)
    if ctx.ticket_graphs is None:
        raise RepoForgeError(
            "TICKET_GRAPH_PROVIDER_UNAVAILABLE: GitHub ticket graph adapter is unavailable",
            code=ErrorCode.TICKET_GRAPH_PROVIDER_UNAVAILABLE,
            retryable=True,
            safe_next_action=(
                "Restore the GitHub ticket-graph provider and retry with fresh=true; "
                "do not modify the reviewed graph configuration."
            ),
        )
    cache = ctx.github_read_cache
    now_epoch = ctx.now_epoch()
    expected_slug = _expected_slug(ctx, repo, source)
    cache_context: dict[str, object] = {"hit_reason": None, "miss_reason": None, "age_ms": None}
    if not fresh and cache is not None:
        cached = cache.get(
            repo.repo_id,
            repo.path,
            "graph",
            source.root_issue,
            ttl_seconds=ctx.config.server.github_read_cache_ttl_seconds,
            now_epoch=now_epoch,
        )
        if cached is None:
            cache_context["miss_reason"] = "no_cache_entry"
        else:
            snapshot = (
                _snapshot_from_payload(cached)
                if _payload_bindings_valid(cached, source, expected_slug)
                else None
            )
            if snapshot is not None:
                age_ms = _observed_age_ms(now_epoch, snapshot.observed_at)
                cache_context["hit_reason"] = "valid_bindings_ttl_fresh"
                cache_context["age_ms"] = age_ms
                return snapshot, True, cache_context
            cache_context["miss_reason"] = (
                "bindings_mismatch"
                if isinstance(cached.get("bindings"), dict)
                else "corrupt_payload"
            )
    elif cache is None:
        cache_context["miss_reason"] = "cache_disabled"
    else:
        cache_context["miss_reason"] = "fresh_requested"
    try:
        snapshot = ctx.ticket_graphs.read(repo.path, source, max_items=200, remote=repo.remote)
    except RepoForgeError as exc:
        if exc.code is ErrorCode.TICKET_GRAPH_PROVIDER_UNAVAILABLE:
            raise
        raise RepoForgeError(
            "TICKET_GRAPH_PROVIDER_UNAVAILABLE: GitHub ticket graph read failed",
            code=ErrorCode.TICKET_GRAPH_PROVIDER_UNAVAILABLE,
            retryable=True,
            safe_next_action=(
                "Repair GitHub authentication or provider availability and retry with fresh=true; "
                "do not rewrite the reviewed graph root."
            ),
        ) from exc
    except Exception as exc:
        raise RepoForgeError(
            "TICKET_GRAPH_PROVIDER_UNAVAILABLE: GitHub ticket graph read failed",
            code=ErrorCode.TICKET_GRAPH_PROVIDER_UNAVAILABLE,
            retryable=True,
            safe_next_action=(
                "Repair GitHub authentication or provider availability and retry with fresh=true; "
                "do not rewrite the reviewed graph root."
            ),
        ) from exc
    if cache is not None:
        cache.put(
            repo.repo_id,
            repo.path,
            "graph",
            source.root_issue,
            _snapshot_payload(snapshot, source),
            now_epoch=now_epoch,
        )
    return snapshot, False, cache_context


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
    valid: bool = True
    diagnostics: list[dict[str, Any]] = field(default_factory=list)
    read_stats: dict[str, Any] | None = None
    coverage: dict[str, Any] = field(default_factory=dict)
    safe_next_action: str | None = None


class RepositoryIssueGraphReader:
    """Bounded, read-only query over one repository's GitHub-native ticket graph."""

    def __init__(self, ctx: ApplicationContext):
        self.ctx = ctx

    def execute(self, c: RepositoryIssueGraphCommand) -> RepositoryIssueGraphResult:
        return self._execute(c, audited=True)

    def compute(self, c: RepositoryIssueGraphCommand) -> RepositoryIssueGraphResult:
        """Read graph state without creating a nested audit event."""
        return self._execute(c, audited=False)

    def _execute(
        self,
        c: RepositoryIssueGraphCommand,
        *,
        audited: bool,
    ) -> RepositoryIssueGraphResult:
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
                    False,
                    [
                        {
                            "code": "GRAPH_NOT_CONFIGURED",
                            "issue_number": 0,
                            "message": (
                                f"Configure repositories.{c.repo_id}.ticket_graph.root_issue"
                            ),
                        }
                    ],
                    read_stats=None,
                    coverage=_coverage(
                        None,
                        diagnostics_count=1,
                        diagnostics_truncated=False,
                    ),
                    safe_next_action=(
                        f"Run `rf repo refresh {c.repo_id} --accept` after adding the reviewed "
                        "ticket_graph root to the source configuration."
                    ),
                )
            snapshot, cache_hit, cache_context = read_github_ticket_snapshot(
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
            diagnostics = [
                {
                    "code": diagnostic.code,
                    "issue_number": diagnostic.issue_number,
                    "message": diagnostic.message,
                }
                for diagnostic in snapshot.diagnostics
            ]
            if not snapshot.evidence_complete:
                diagnostics.append(_incomplete_graph_diagnostic(snapshot))
            diagnostics_truncated = len(diagnostics) > _DIAGNOSTICS_LIMIT
            diagnostics = diagnostics[:_DIAGNOSTICS_LIMIT]
            evolution = _evolution_by_number(snapshot)
            cache_age_ms = cache_context.get("age_ms")
            cache_age_value = (
                float(cache_age_ms) if isinstance(cache_age_ms, (int, float)) else None
            )
            return RepositoryIssueGraphResult(
                c.repo_id,
                "github",
                cache_hit,
                snapshot.graph.program_issue,
                snapshot.observed_at,
                snapshot.evidence_complete,
                list(snapshot.unavailable),
                [{**node_payload(node), "evolution": evolution.get(node.number)} for node in nodes],
                len(nodes),
                truncated,
                snapshot.evidence_complete,
                diagnostics,
                read_stats=(
                    _cache_hit_read_stats(
                        cache_hit_reason=str(cache_context.get("hit_reason") or "cache_hit"),
                        cache_age_ms=cache_age_value,
                    )
                    if cache_hit
                    else _read_stats_payload(
                        snapshot.read_stats,
                        cache_miss_reason=(
                            str(cache_context.get("miss_reason"))
                            if cache_context.get("miss_reason")
                            else None
                        ),
                    )
                ),
                coverage=_coverage(
                    repo.ticket_graph.root_issue if repo.ticket_graph else c.root_issue,
                    snapshot,
                    diagnostics_count=len(diagnostics),
                    diagnostics_truncated=diagnostics_truncated,
                ),
                safe_next_action=(
                    None
                    if snapshot.evidence_complete
                    else (
                        "Retry with `fresh=true`; if evidence remains incomplete, verify GitHub "
                        "sub-issue and dependency API access before selecting work."
                    )
                ),
            )

        return self.ctx.audited("repo_issue_graph", details, op) if audited else op()
