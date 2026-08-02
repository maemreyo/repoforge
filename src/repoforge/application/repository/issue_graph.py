"""Repository ticket-graph use case: bounded read and audit-wrapped execution.

Kept focused on orchestration after the F-006 split:

- ``issue_graph_payloads`` owns dict-payload serialization and coverage helpers;
- ``issue_graph_cache`` owns the versioned cache envelope codec and bindings.

This module re-exports the names historical consumers import
(``node_payload``, ``capability_coverage_payload``,
``is_capability_complete_for_issue``, ``read_github_ticket_snapshot`` and the
private ``_*`` helpers used by ``issue_next`` and tests).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ...config import RepositoryConfig
from ...domain.errors import ErrorCode, RepoForgeError
from ...domain.tickets import (
    TicketGraphError,
    TicketGraphSnapshot,
    TicketPriority,
    TicketStatus,
)
from ..context import ApplicationContext
from ..tickets.graph import select_ticket_nodes
from .issue_graph_cache import (
    observed_age_ms as _observed_age_ms,
)
from .issue_graph_cache import (
    payload_bindings_valid as _payload_bindings_valid,
)
from .issue_graph_cache import (
    snapshot_from_payload as _snapshot_from_payload,
)
from .issue_graph_cache import (
    snapshot_payload as _snapshot_payload,
)
from .issue_graph_config import expected_slug as resolve_expected_slug
from .issue_graph_config import source as resolve_source
from .issue_graph_payloads import (
    cache_hit_read_stats as _cache_hit_read_stats,
)
from .issue_graph_payloads import (
    capability_coverage_payload,
    is_capability_complete_for_issue,
    node_payload,
)
from .issue_graph_payloads import (
    coverage_payload as _coverage,
)
from .issue_graph_payloads import (
    evolution_by_number as _evolution_by_number,
)
from .issue_graph_payloads import (
    incomplete_graph_diagnostic as _incomplete_graph_diagnostic,
)
from .issue_graph_payloads import (
    read_stats_payload as _read_stats_payload,
)

_DIAGNOSTICS_LIMIT = 100

__all__ = [
    "RepositoryIssueGraphCommand",
    "RepositoryIssueGraphReader",
    "RepositoryIssueGraphResult",
    "_cache_hit_read_stats",
    "_coverage",
    "_evolution_by_number",
    "_incomplete_graph_diagnostic",
    "_observed_age_ms",
    "_read_stats_payload",
    "_snapshot_payload",
    "capability_coverage_payload",
    "is_capability_complete_for_issue",
    "node_payload",
    "read_github_ticket_snapshot",
]


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
    source = resolve_source(repo, root_issue)
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
    expected_slug = resolve_expected_slug(ctx, repo, source)
    authority_digest = ctx.config.server.github_read_cache_authority_digest
    cache_context: dict[str, object] = {"hit_reason": None, "miss_reason": None, "age_ms": None}
    if not fresh and cache is not None and authority_digest is not None:
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
                if _payload_bindings_valid(cached, source, expected_slug, authority_digest)
                else None
            )
            if snapshot is not None:
                age_ms = _observed_age_ms(now_epoch, snapshot.observed_at)
                if age_ms is None:
                    # The cached envelope's age cannot be trusted (future
                    # timestamp beyond the allowed skew, or unparseable):
                    # fall through and force a fresh read instead of serving
                    # evidence whose age would violate the output contract.
                    cache_context["miss_reason"] = "clock_skew"
                else:
                    cache_context["hit_reason"] = "valid_bindings_ttl_fresh"
                    cache_context["age_ms"] = age_ms
                    return snapshot, True, cache_context
            else:
                cache_context["miss_reason"] = (
                    "bindings_mismatch"
                    if isinstance(cached.get("bindings"), dict)
                    else "corrupt_payload"
                )
    elif fresh:
        cache_context["miss_reason"] = "fresh_requested"
    elif cache is None:
        cache_context["miss_reason"] = "cache_disabled"
    else:
        cache_context["miss_reason"] = "authority_not_pinned"
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
    if cache is not None and authority_digest is not None:
        cache.put(
            repo.repo_id,
            repo.path,
            "graph",
            source.root_issue,
            _snapshot_payload(snapshot, source, authority_digest),
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
