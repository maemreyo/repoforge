"""Pure derived ticket readiness, WIP, and deterministic delivery ordering."""

from __future__ import annotations

from ...domain.tickets import (
    RequirementRelationType,
    TicketDiagnostic,
    TicketGraph,
    TicketGraphError,
    TicketLiveState,
    TicketNode,
    TicketPriority,
    TicketReadinessAssessment,
    TicketReadinessPolicy,
    TicketReadinessReport,
    TicketStatus,
    TicketType,
)
from .graph import validate_ticket_graph

_PRIORITY_ORDER = {
    TicketPriority.P0: 0,
    TicketPriority.P1: 1,
    TicketPriority.P2: 2,
    TicketPriority.P3: 3,
}
_ACTIVE_PARENT_STATUSES = {
    TicketStatus.READY,
    TicketStatus.IN_PROGRESS,
    TicketStatus.IN_REVIEW,
}
_ACTIVE_WIP_STATUSES = {TicketStatus.IN_PROGRESS, TicketStatus.IN_REVIEW}
_ADVISORY_GRAPH_DIAGNOSTICS = {"READY_WITH_OPEN_BLOCKER"}

_REASON_MESSAGES = {
    "LIVE_METADATA_UNAVAILABLE": "Live issue state could not be read, so readiness fails closed.",
    "SUPERSEDED": "The ticket has an explicit superseding issue.",
    "PARTIAL_COMPLETION_REMAINS": "The closed or handed-off result still has explicit remaining or unverified scope.",
    "INVALIDATED_ASSUMPTION": "Another requirement explicitly invalidated an assumption used by this ticket.",
    "SPECIFICATION_INCOMPLETE": "The ticket specification is incomplete.",
    "DESIGN_GATE_UNRESOLVED": "A required design decision is still unresolved.",
    "PARENT_INACTIVE": "The parent program or initiative is not active.",
    "OPEN_BLOCKERS": "One or more blocker issues are not closed.",
    "PRIORITY_WIP_LIMIT": "The configured WIP limit for this priority is already reached.",
    "INITIATIVE_WIP_LIMIT": "The configured WIP limit for this initiative is already reached.",
}


def _live_map(live_states: tuple[TicketLiveState, ...]) -> dict[int, TicketLiveState]:
    result: dict[int, TicketLiveState] = {}
    for item in live_states:
        if item.number in result:
            raise TicketGraphError(f"duplicate live ticket state for issue #{item.number}")
        result[item.number] = item
    return result


def _replacement_targets(state: TicketLiveState) -> tuple[int, ...]:
    targets = {
        item.target_issue
        for item in state.delivery.relations
        if item.relation_type
        in {RequirementRelationType.SUPERSEDED_BY, RequirementRelationType.MERGED_INTO}
    }
    if state.delivery.superseded_by is not None:
        targets.add(state.delivery.superseded_by)
    return tuple(sorted(targets))


def _base_status(node: TicketNode, live: TicketLiveState | None) -> TicketStatus:
    if live is None or live.is_open is None:
        return TicketStatus.BLOCKED
    if _replacement_targets(live) or node.status is TicketStatus.SUPERSEDED:
        return TicketStatus.SUPERSEDED
    if live.is_open is False:
        if (
            live.delivery.partial_completion is not None
            and live.delivery.partial_completion.has_remaining_scope
        ):
            return TicketStatus.BLOCKED
        return TicketStatus.DONE
    return node.status


def _evolution_diagnostics(
    live_states: tuple[TicketLiveState, ...],
) -> tuple[TicketDiagnostic, ...]:
    diagnostics: list[TicketDiagnostic] = []
    edges: dict[int, set[int]] = {}
    for state in live_states:
        replacements = set(_replacement_targets(state))
        for relation in state.delivery.relations:
            if relation.target_issue == state.number:
                diagnostics.append(
                    TicketDiagnostic(
                        "SELF_REQUIREMENT_RELATION",
                        state.number,
                        "requirement evolution cannot target the same issue",
                    )
                )
            if relation.relation_type is RequirementRelationType.SUPERSEDES:
                edges.setdefault(relation.target_issue, set()).add(state.number)
            elif relation.relation_type in {
                RequirementRelationType.SUPERSEDED_BY,
                RequirementRelationType.MERGED_INTO,
                RequirementRelationType.SPLIT_INTO,
            }:
                edges.setdefault(state.number, set()).add(relation.target_issue)
        if len(replacements) > 1:
            diagnostics.append(
                TicketDiagnostic(
                    "AMBIGUOUS_SUPERSESSION",
                    state.number,
                    f"ticket declares multiple canonical replacements: {sorted(replacements)}",
                )
            )

    visiting: set[int] = set()
    visited: set[int] = set()
    stack: list[int] = []
    cycle_nodes: set[int] = set()

    def visit(number: int) -> None:
        if number in visited:
            return
        if number in visiting:
            try:
                start = stack.index(number)
            except ValueError:
                start = 0
            cycle_nodes.update(stack[start:])
            return
        visiting.add(number)
        stack.append(number)
        for target in sorted(edges.get(number, ())):
            visit(target)
        stack.pop()
        visiting.remove(number)
        visited.add(number)

    for number in sorted(edges):
        visit(number)
    diagnostics.extend(
        TicketDiagnostic(
            "SUPERSESSION_CYCLE",
            number,
            "ticket participates in a supersession, split, or merge cycle",
        )
        for number in sorted(cycle_nodes)
    )
    return tuple(sorted(set(diagnostics)))


def _invalidated_targets(live_states: tuple[TicketLiveState, ...]) -> frozenset[int]:
    return frozenset(
        relation.target_issue
        for state in live_states
        for relation in state.delivery.relations
        if relation.relation_type is RequirementRelationType.INVALIDATES
    )


def _initiative_number(node: TicketNode, nodes: dict[int, TicketNode]) -> int | None:
    parent = node.parent
    visited: set[int] = set()
    while parent is not None and parent not in visited:
        visited.add(parent)
        ancestor = nodes.get(parent)
        if ancestor is None:
            return None
        if ancestor.ticket_type is TicketType.INITIATIVE:
            return ancestor.number
        parent = ancestor.parent
    return None


def _metadata_repairs(node: TicketNode, derived: TicketStatus) -> tuple[str, ...]:
    if node.status is derived:
        return ()
    return (f"status: {node.status.value} -> {derived.value}",)


def _assessment(
    node: TicketNode,
    *,
    derived: TicketStatus,
    reason_codes: tuple[str, ...] = (),
    unresolved_blockers: tuple[int, ...] = (),
    wip_conflicts: tuple[int, ...] = (),
    wave: int = 0,
    sequence: int = 0,
) -> TicketReadinessAssessment:
    return TicketReadinessAssessment(
        number=node.number,
        declared_status=node.status,
        derived_status=derived,
        selectable=(
            node.ticket_type is TicketType.IMPLEMENTATION_TICKET
            and derived is TicketStatus.READY
            and not reason_codes
        ),
        reason_codes=reason_codes,
        reasons=tuple(_REASON_MESSAGES[code] for code in reason_codes),
        unresolved_blockers=unresolved_blockers,
        wip_conflicts=wip_conflicts,
        metadata_repairs=_metadata_repairs(node, derived),
        wave=wave,
        sequence=sequence,
    )


def derive_ticket_readiness(
    graph: TicketGraph,
    live_states: tuple[TicketLiveState, ...],
    *,
    policy: TicketReadinessPolicy | None = None,
) -> TicketReadinessReport:
    """Derive advisory delivery state without editing GitHub or the manifest.

    Structural graph defects are fatal to selection. Stale declared status is not:
    open/closed state, specification metadata, design gates, blockers, parent activity,
    and WIP limits produce explicit per-ticket reasons instead.
    """

    readiness_policy = policy or TicketReadinessPolicy()
    diagnostics = tuple(
        item
        for item in validate_ticket_graph(graph)
        if item.code not in _ADVISORY_GRAPH_DIAGNOSTICS
    ) + _evolution_diagnostics(live_states)
    if diagnostics:
        return TicketReadinessReport((), (), diagnostics)

    nodes = {node.number: node for node in graph.nodes}
    live = _live_map(live_states)
    base_status = {number: _base_status(node, live.get(number)) for number, node in nodes.items()}
    invalidated_targets = _invalidated_targets(live_states)
    initiative_by_ticket = {
        number: _initiative_number(node, nodes) for number, node in nodes.items()
    }
    active_ticket_ids = tuple(
        sorted(
            node.number
            for node in graph.nodes
            if node.ticket_type is TicketType.IMPLEMENTATION_TICKET
            and base_status[node.number] in _ACTIVE_WIP_STATUSES
            and live.get(node.number) is not None
            and live[node.number].is_open is True
        )
    )

    assessments: list[TicketReadinessAssessment] = []
    for node in sorted(graph.nodes, key=lambda item: item.number):
        state = live.get(node.number)
        wave = state.delivery.wave if state is not None else 0
        sequence = state.delivery.sequence if state is not None else 0
        initial = base_status[node.number]

        if state is None or state.is_open is None:
            assessments.append(
                _assessment(
                    node,
                    derived=TicketStatus.BLOCKED,
                    reason_codes=("LIVE_METADATA_UNAVAILABLE",),
                    wave=wave,
                    sequence=sequence,
                )
            )
            continue
        if initial is TicketStatus.SUPERSEDED:
            assessments.append(
                _assessment(
                    node,
                    derived=TicketStatus.SUPERSEDED,
                    reason_codes=("SUPERSEDED",),
                    wave=wave,
                    sequence=sequence,
                )
            )
            continue
        if (
            state.delivery.partial_completion is not None
            and state.delivery.partial_completion.has_remaining_scope
        ):
            assessments.append(
                _assessment(
                    node,
                    derived=TicketStatus.BLOCKED,
                    reason_codes=("PARTIAL_COMPLETION_REMAINS",),
                    wave=wave,
                    sequence=sequence,
                )
            )
            continue
        if node.number in invalidated_targets:
            assessments.append(
                _assessment(
                    node,
                    derived=TicketStatus.BLOCKED,
                    reason_codes=("INVALIDATED_ASSUMPTION",),
                    wave=wave,
                    sequence=sequence,
                )
            )
            continue
        if initial is TicketStatus.DONE:
            assessments.append(
                _assessment(
                    node,
                    derived=TicketStatus.DONE,
                    wave=wave,
                    sequence=sequence,
                )
            )
            continue
        if node.ticket_type is not TicketType.IMPLEMENTATION_TICKET:
            assessments.append(
                _assessment(
                    node,
                    derived=initial,
                    wave=wave,
                    sequence=sequence,
                )
            )
            continue

        reason_codes: list[str] = []
        if not state.delivery.specification_complete:
            reason_codes.append("SPECIFICATION_INCOMPLETE")
        if state.delivery.unresolved_design_gate:
            reason_codes.append("DESIGN_GATE_UNRESOLVED")

        parent_status = base_status.get(node.parent) if node.parent is not None else None
        if parent_status not in _ACTIVE_PARENT_STATUSES:
            reason_codes.append("PARENT_INACTIVE")

        unresolved_blockers = tuple(
            blocker
            for blocker in node.blockers
            if blocker not in live or live[blocker].is_open is not False
        )
        if unresolved_blockers:
            reason_codes.append("OPEN_BLOCKERS")

        if reason_codes:
            assessments.append(
                _assessment(
                    node,
                    derived=TicketStatus.BLOCKED,
                    reason_codes=tuple(reason_codes),
                    unresolved_blockers=unresolved_blockers,
                    wave=wave,
                    sequence=sequence,
                )
            )
            continue

        if initial in _ACTIVE_WIP_STATUSES:
            assessments.append(
                _assessment(
                    node,
                    derived=initial,
                    wave=wave,
                    sequence=sequence,
                )
            )
            continue

        priority_conflicts = tuple(
            issue_number
            for issue_number in active_ticket_ids
            if nodes[issue_number].priority is node.priority
        )
        initiative = initiative_by_ticket[node.number]
        initiative_conflicts = tuple(
            issue_number
            for issue_number in active_ticket_ids
            if initiative is not None and initiative_by_ticket[issue_number] == initiative
        )
        wip_reason_codes: list[str] = []
        conflicts: set[int] = set()
        if len(priority_conflicts) >= readiness_policy.priority_limit(node.priority):
            wip_reason_codes.append("PRIORITY_WIP_LIMIT")
            conflicts.update(priority_conflicts)
        if (
            initiative is not None
            and len(initiative_conflicts) >= readiness_policy.initiative_limit
        ):
            wip_reason_codes.append("INITIATIVE_WIP_LIMIT")
            conflicts.update(initiative_conflicts)
        if wip_reason_codes:
            assessments.append(
                _assessment(
                    node,
                    derived=TicketStatus.BLOCKED,
                    reason_codes=tuple(wip_reason_codes),
                    wip_conflicts=tuple(sorted(conflicts)),
                    wave=wave,
                    sequence=sequence,
                )
            )
            continue

        assessments.append(
            _assessment(
                node,
                derived=TicketStatus.READY,
                wave=wave,
                sequence=sequence,
            )
        )

    by_number = {assessment.number: assessment for assessment in assessments}
    recommended_nodes = [node for node in graph.nodes if by_number[node.number].selectable]
    recommended_nodes.sort(
        key=lambda node: (
            _PRIORITY_ORDER[node.priority],
            by_number[node.number].wave,
            by_number[node.number].sequence,
            node.number,
        )
    )
    return TicketReadinessReport(
        assessments=tuple(assessments),
        recommended=tuple(node.number for node in recommended_nodes),
        diagnostics=(),
    )
