from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ...domain.tickets import (
    TicketDeliveryMetadata,
    TicketDiagnostic,
    TicketGraphError,
    TicketLiveState,
    TicketReadinessAssessment,
    TicketReadinessPolicy,
)
from ..context import ApplicationContext
from ..tickets.graph import ticket_subtree_numbers, validate_ticket_graph
from ..tickets.live import ticket_live_state_from_issue
from ..tickets.readiness import derive_ticket_readiness
from ..tickets.repo_manifest import load_repo_ticket_graph
from .issue_graph import node_payload

_MAX_LIVE_ISSUES = 200
_MAX_LIVE_WORKERS = 8


def _diagnostic_payload(item: TicketDiagnostic) -> dict[str, Any]:
    return {
        "code": item.code,
        "issue_number": item.issue_number,
        "message": item.message,
    }


def _assessment_payload(item: TicketReadinessAssessment) -> dict[str, Any]:
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
    }


def _unavailable_live_state(number: int) -> TicketLiveState:
    return TicketLiveState(
        number,
        None,
        TicketDeliveryMetadata(specification_complete=False),
    )


def _read_live_states(
    ctx: ApplicationContext,
    repo_path: Path,
    issue_numbers: tuple[int, ...],
) -> tuple[TicketLiveState, ...]:
    if not issue_numbers or len(issue_numbers) > _MAX_LIVE_ISSUES:
        raise TicketGraphError(
            f"derived readiness requires between 1 and {_MAX_LIVE_ISSUES} bounded live issues"
        )

    def read_one(number: int) -> TicketLiveState:
        try:
            payload = ctx.github.issue_read(repo_path, number)
            return ticket_live_state_from_issue(payload, expected_number=number)
        except Exception:
            return _unavailable_live_state(number)

    def read_all() -> tuple[TicketLiveState, ...]:
        workers = min(_MAX_LIVE_WORKERS, len(issue_numbers))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            states = tuple(pool.map(read_one, issue_numbers))
        return tuple(sorted(states, key=lambda item: item.number))

    return ctx.audited(
        "repo_issue_next_live",
        {"issue_count": len(issue_numbers)},
        read_all,
        mutating=False,
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


@dataclass(frozen=True, slots=True)
class RepositoryIssueNextResult:
    repo_id: str
    manifest_found: bool
    valid: bool
    diagnostics: list[dict[str, Any]]
    tickets: list[dict[str, Any]]
    assessments: list[dict[str, Any]]
    metadata_repairs: list[dict[str, Any]]


class RepositoryIssueNextReader:
    """Derive advisory delivery readiness from the graph and bounded live issue state."""

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
            if not isinstance(c.limit, int) or isinstance(c.limit, bool) or not 1 <= c.limit <= 100:
                raise TicketGraphError("limit must be between 1 and 100")
            graph = load_repo_ticket_graph(repo.path)
            if graph is None:
                details["manifest_found"] = False
                details["valid"] = False
                details["ticket_count"] = 0
                return RepositoryIssueNextResult(c.repo_id, False, False, [], [], [], [])

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
                    [_diagnostic_payload(item) for item in diagnostics],
                    [],
                    [],
                    [],
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
                details["manifest_found"] = True
                details["valid"] = False
                details["diagnostic_count"] = 1
                details["ticket_count"] = 0
                return RepositoryIssueNextResult(
                    c.repo_id,
                    True,
                    False,
                    [_diagnostic_payload(diagnostic)],
                    [],
                    [],
                    [],
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
            live_states = _read_live_states(
                self.ctx,
                repo.path,
                tuple(sorted(node.number for node in graph.nodes)),
            )
            report = derive_ticket_readiness(graph, live_states, policy=policy)
            if report.diagnostics:
                details["manifest_found"] = True
                details["valid"] = False
                details["diagnostic_count"] = len(report.diagnostics)
                details["ticket_count"] = 0
                return RepositoryIssueNextResult(
                    c.repo_id,
                    True,
                    False,
                    [_diagnostic_payload(item) for item in report.diagnostics],
                    [],
                    [],
                    [],
                )

            nodes = {node.number: node for node in graph.nodes}
            assessments = {item.number: item for item in report.assessments}
            recommended = [number for number in report.recommended if number in scope][: c.limit]
            tickets = [
                {
                    **node_payload(nodes[number]),
                    "readiness": _assessment_payload(assessments[number]),
                }
                for number in recommended
            ]
            assessment_payloads = [
                _assessment_payload(item) for item in report.assessments if item.number in scope
            ]
            repairs = [
                {"issue_number": item.number, "repairs": list(item.metadata_repairs)}
                for item in report.assessments
                if item.number in scope and item.metadata_repairs
            ]
            details["manifest_found"] = True
            details["valid"] = True
            details["ticket_count"] = len(tickets)
            return RepositoryIssueNextResult(
                c.repo_id,
                True,
                True,
                [],
                tickets,
                assessment_payloads,
                repairs,
            )

        return self.ctx.audited("repo_issue_next", details, op)
