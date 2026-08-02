"""Serialization and coverage payloads for the ticket-graph use case.

Pure dict-payload builders for graph nodes, capability coverage, diagnostics,
evolution metadata, and read stats.  Kept separate from the cache envelope
(``issue_graph_cache``) and the read/command orchestration (``issue_graph``)
so each module stays under the 400-line source budget (F-006).
"""

from __future__ import annotations

from typing import Any

from ...domain.tickets import (
    CapabilityCoverage,
    GraphEvidenceCapability,
    TicketGraphReadStats,
    TicketGraphSnapshot,
    TicketNode,
)
from ..tickets.live import ticket_delivery_payload, ticket_live_state_from_issue


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

    Fail-closed and scope-aware (F-001):

    - ``truncated`` is a *global* failure: every candidate that depends on the
      capability is insufficient, because truncation hides evidence the
      candidate cannot see.
    - an issue listed in ``unavailable`` is a *localized* failure: only that
      candidate is insufficient; others are unaffected.
    - ``complete=False`` with an empty unavailable list is an inconsistent
      envelope and fails closed.
    - a missing coverage entry is *not* treated as complete — for a required
      capability, absence of evidence is insufficiency, not sufficiency.
    """
    if snapshot is None:
        return False
    for coverage in snapshot.capability_coverage:
        if coverage.capability is capability:
            if coverage.truncated:
                return False
            if issue_number in coverage.unavailable:
                return False
            return coverage.complete or bool(coverage.unavailable)
    return False


def coverage_payload(
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


def incomplete_graph_diagnostic(snapshot: TicketGraphSnapshot) -> dict[str, Any]:
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


def evolution_by_number(snapshot: TicketGraphSnapshot) -> dict[int, dict[str, object]]:
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


def read_stats_payload(
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


def cache_hit_read_stats(*, cache_hit_reason: str, cache_age_ms: float | None) -> dict[str, Any]:
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


__all__ = [
    "cache_hit_read_stats",
    "capability_coverage_payload",
    "coverage_payload",
    "evolution_by_number",
    "incomplete_graph_diagnostic",
    "is_capability_complete_for_issue",
    "node_payload",
    "read_stats_payload",
]
