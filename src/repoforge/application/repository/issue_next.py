from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ...domain.observation import EvidenceRequirement, EvidenceVerdict
from ...domain.tickets import (
    TicketDeliveryMetadata,
    TicketDiagnostic,
    TicketGraphError,
    TicketGraphSnapshot,
    TicketLiveState,
    TicketReadinessAssessment,
    TicketReadinessPolicy,
)
from ..context import ApplicationContext
from ..tickets.graph import ticket_subtree_numbers, validate_ticket_graph
from ..tickets.live import ticket_delivery_payload, ticket_live_state_from_issue
from ..tickets.readiness import derive_ticket_readiness
from .evidence_evaluator import evaluate_candidate
from .issue_graph import (
    _cache_hit_read_stats,
    _incomplete_graph_diagnostic,
    _read_stats_payload,
    capability_coverage_payload,
    node_payload,
    read_github_ticket_snapshot,
)

_MAX_LIVE_ISSUES = 200


def _diagnostic_payload(item: TicketDiagnostic) -> dict[str, Any]:
    return {
        "code": item.code,
        "issue_number": item.issue_number,
        "message": item.message,
    }


def _assessment_payload(
    item: TicketReadinessAssessment,
    delivery: TicketDeliveryMetadata | None = None,
) -> dict[str, Any]:
    return {
        "number": item.number,
        "declared_status": item.declared_status.value,
        "derived_status": item.derived_status.value,
        "selectable": item.selectable,
        "reason_codes": list(item.reason_codes),
        "reasons": list(item.reasons),
        "unresolved_blockers": list(item.unresolved_blockers),
        "wip_conflicts": list(item.wip_conflicts),
        "metadata_repairs": list(item.metadata_repairs),
        "wave": item.wave,
        "sequence": item.sequence,
        "evolution": ticket_delivery_payload(
            delivery or TicketDeliveryMetadata(specification_complete=False)
        ),
    }


def _unavailable_live_state(number: int) -> TicketLiveState:
    return TicketLiveState(
        number,
        None,
        TicketDeliveryMetadata(specification_complete=False),
    )


def _live_states(snapshot: TicketGraphSnapshot) -> tuple[TicketLiveState, ...]:
    by_number = {
        issue.number: ticket_live_state_from_issue(
            {
                "number": issue.number,
                "title": issue.title,
                "state": issue.state,
                "body": issue.body,
                "comments": [{"body": body} for body in issue.comments],
            },
            expected_number=issue.number,
        )
        for issue in snapshot.live_issues
    }
    return tuple(
        by_number.get(node.number, _unavailable_live_state(node.number))
        for node in snapshot.graph.nodes
    )


@dataclass(frozen=True, slots=True)
class RepositoryIssueNextCommand:
    repo_id: str
    root_issue: int | None = None
    limit: int = 1
    p0_wip_limit: int = 2
    p1_wip_limit: int = 3
    p2_wip_limit: int = 4
    p3_wip_limit: int = 4
    initiative_wip_limit: int = 2
    fresh: bool = False


@dataclass(frozen=True, slots=True)
class RepositoryIssueNextResult:
    repo_id: str
    source: str
    cache_hit: bool
    observed_at: str
    evidence_complete: bool
    unavailable: list[int]
    valid: bool
    diagnostics: list[dict[str, Any]]
    tickets: list[dict[str, Any]]
    assessments: list[dict[str, Any]]
    metadata_repairs: list[dict[str, Any]]
    capability_coverage: list[dict[str, Any]] = field(default_factory=list)
    read_stats: dict[str, Any] | None = None


class RepositoryIssueNextReader:
    """Derive advisory readiness from one consistent GitHub graph observation."""

    def __init__(self, ctx: ApplicationContext):
        self.ctx = ctx

    def execute(self, c: RepositoryIssueNextCommand) -> RepositoryIssueNextResult:
        return self._execute(c, audited=True)

    def compute(self, c: RepositoryIssueNextCommand) -> RepositoryIssueNextResult:
        """Derive readiness without creating a nested audit event."""
        return self._execute(c, audited=False)

    def _execute(
        self,
        c: RepositoryIssueNextCommand,
        *,
        audited: bool,
    ) -> RepositoryIssueNextResult:
        details: dict[str, object] = {
            "repo_id": c.repo_id,
            "root_issue": c.root_issue,
            "limit": c.limit,
            "fresh": c.fresh,
        }

        def result(
            snapshot: TicketGraphSnapshot,
            cache_hit: bool,
            cache_context: dict[str, object],
            *,
            valid: bool,
            diagnostics: list[dict[str, Any]],
            tickets: list[dict[str, Any]],
            assessments: list[dict[str, Any]],
            repairs: list[dict[str, Any]],
        ) -> RepositoryIssueNextResult:
            cache_age_ms = cache_context.get("age_ms")
            cache_age_value = (
                float(cache_age_ms) if isinstance(cache_age_ms, (int, float)) else None
            )
            return RepositoryIssueNextResult(
                c.repo_id,
                "github",
                cache_hit,
                snapshot.observed_at,
                snapshot.evidence_complete,
                list(snapshot.unavailable),
                valid,
                diagnostics,
                tickets,
                assessments,
                repairs,
                capability_coverage_payload(snapshot),
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
            )

        def op() -> RepositoryIssueNextResult:
            if not isinstance(c.limit, int) or isinstance(c.limit, bool) or not 1 <= c.limit <= 100:
                raise TicketGraphError("limit must be between 1 and 100")
            repo = self.ctx.repo(c.repo_id)
            if repo.ticket_graph is None and c.root_issue is None:
                details.update(
                    source="github",
                    cache_hit=False,
                    evidence_complete=False,
                    valid=False,
                    diagnostic_count=1,
                    ticket_count=0,
                )
                return RepositoryIssueNextResult(
                    c.repo_id,
                    "github",
                    False,
                    self.ctx.clock.now_iso(),
                    False,
                    [],
                    False,
                    [
                        {
                            "code": "GRAPH_NOT_CONFIGURED",
                            "issue_number": 0,
                            "message": "Configure repositories.<id>.ticket_graph.root_issue",
                        }
                    ],
                    [],
                    [],
                    [],
                )
            snapshot, cache_hit, cache_context = read_github_ticket_snapshot(
                self.ctx,
                repo,
                root_issue=c.root_issue,
                fresh=c.fresh,
            )
            graph = snapshot.graph
            details["source"] = "github"
            details["cache_hit"] = cache_hit
            details["evidence_complete"] = snapshot.evidence_complete
            diagnostics = validate_ticket_graph(graph)
            if diagnostics:
                details["valid"] = False
                details["diagnostic_count"] = len(diagnostics)
                details["ticket_count"] = 0
                return result(
                    snapshot,
                    cache_hit,
                    cache_context,
                    valid=False,
                    diagnostics=[_diagnostic_payload(item) for item in diagnostics],
                    tickets=[],
                    assessments=[],
                    repairs=[],
                )
            if len(graph.nodes) > _MAX_LIVE_ISSUES:
                diagnostic = TicketDiagnostic(
                    "LIVE_GRAPH_TOO_LARGE",
                    graph.program_issue,
                    (
                        f"ticket graph has {len(graph.nodes)} nodes; live readiness is bounded "
                        f"to {_MAX_LIVE_ISSUES}"
                    ),
                )
                details["valid"] = False
                details["diagnostic_count"] = 1
                details["ticket_count"] = 0
                return result(
                    snapshot,
                    cache_hit,
                    cache_context,
                    valid=False,
                    diagnostics=[_diagnostic_payload(diagnostic)],
                    tickets=[],
                    assessments=[],
                    repairs=[],
                )

            scope = (
                ticket_subtree_numbers(graph, c.root_issue)
                if c.root_issue is not None
                else frozenset(node.number for node in graph.nodes)
            )
            policy = TicketReadinessPolicy(
                p0_limit=c.p0_wip_limit,
                p1_limit=c.p1_wip_limit,
                p2_limit=c.p2_wip_limit,
                p3_limit=c.p3_wip_limit,
                initiative_limit=c.initiative_wip_limit,
            )
            live_states = _live_states(snapshot)
            live_by_number = {item.number: item for item in live_states}
            report = derive_ticket_readiness(graph, live_states, policy=policy)
            if report.diagnostics:
                details["valid"] = False
                details["diagnostic_count"] = len(report.diagnostics)
                details["ticket_count"] = 0
                return result(
                    snapshot,
                    cache_hit,
                    cache_context,
                    valid=False,
                    diagnostics=[_diagnostic_payload(item) for item in report.diagnostics],
                    tickets=[],
                    assessments=[],
                    repairs=[],
                )

            nodes = {node.number: node for node in graph.nodes}
            assessments = {item.number: item for item in report.assessments}
            ranked = [number for number in report.recommended if number in scope]
            requirement = EvidenceRequirement.for_next_default()
            selectable: list[int] = []
            blocked: list[tuple[int, EvidenceVerdict]] = []
            for number in ranked:
                verdict = evaluate_candidate(
                    snapshot, requirement, number, now_epoch=self.ctx.now_epoch()
                )
                if verdict.sufficient:
                    selectable.append(number)
                    if len(selectable) == c.limit:
                        break
                else:
                    blocked.append((number, verdict))
            blocked_diagnostics = [
                {
                    "code": "CANDIDATE_EVIDENCE_INSUFFICIENT",
                    "issue_number": number,
                    "message": " ".join(verdict.diagnostics),
                }
                for number, verdict in blocked
            ]
            if not selectable and blocked:
                incomplete_diagnostic = _incomplete_graph_diagnostic(snapshot)
                snapshot_diagnostics = [_diagnostic_payload(item) for item in snapshot.diagnostics]
                combined = [
                    *snapshot_diagnostics,
                    incomplete_diagnostic,
                    *blocked_diagnostics,
                ]
                details["valid"] = False
                details["diagnostic_count"] = len(combined)
                details["ticket_count"] = 0
                return result(
                    snapshot,
                    cache_hit,
                    cache_context,
                    valid=False,
                    diagnostics=combined,
                    tickets=[],
                    assessments=[],
                    repairs=[],
                )
            tickets = [
                {
                    **node_payload(nodes[number]),
                    "evolution": ticket_delivery_payload(live_by_number[number].delivery),
                    "readiness": _assessment_payload(
                        assessments[number], live_by_number[number].delivery
                    ),
                }
                for number in selectable
            ]
            assessment_payloads = [
                _assessment_payload(item, live_by_number[item.number].delivery)
                for item in report.assessments
                if item.number in scope
            ]
            repairs = [
                {"issue_number": item.number, "repairs": list(item.metadata_repairs)}
                for item in report.assessments
                if item.number in scope and item.metadata_repairs
            ]
            details["valid"] = True
            details["ticket_count"] = len(tickets)
            return result(
                snapshot,
                cache_hit,
                cache_context,
                valid=True,
                diagnostics=blocked_diagnostics,
                tickets=tickets,
                assessments=assessment_payloads,
                repairs=repairs,
            )

        return self.ctx.audited("repo_issue_next", details, op) if audited else op()
