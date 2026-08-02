"""TicketNode and TicketLiveMetadata assembly from parsed expanded issues.

Maps parent/children/blockers/blocked-by relationships into the adjacency
maps that ``TicketNode`` needs, and builds the final node and live-metadata
lists with status/priority/type defaulting.
"""

from __future__ import annotations

from ...config import GitHubTicketGraphConfig
from ...domain.tickets import (
    CapabilityCoverage,
    GraphEvidenceCapability,
    TicketDiagnostic,
    TicketLiveMetadata,
    TicketNode,
    TicketPriority,
    TicketStatus,
    TicketType,
)
from .graph_decode import ExpandedIssue, enum_value, parse_metadata


def build_adjacency_maps(
    wanted: set[int],
    parent_by_number: dict[int, int | None],
    expanded: dict[int, ExpandedIssue],
) -> tuple[
    dict[int, set[int]],
    dict[int, set[int]],
    dict[int, set[int]],
]:
    """Derive children / blockers / blocks maps from the BFS parent tree and
    each expanded issue's raw edge numbers.

    Returns ``(children_by_number, blockers_by_number, blocks_by_number)``
    where every key in ``wanted`` has an entry (possibly empty).
    """
    children_by_number: dict[int, set[int]] = {number: set() for number in wanted}
    for child_number, parent_number in parent_by_number.items():
        if child_number in wanted and parent_number in wanted:
            children_by_number[parent_number].add(child_number)

    blockers_by_number: dict[int, set[int]] = {number: set() for number in wanted}
    for number in wanted:
        blockers_by_number[number] = {
            blocker for blocker in expanded[number].blockers if blocker in wanted
        }

    blocks_by_number: dict[int, set[int]] = {number: set() for number in wanted}
    for blocked_number, blocker_numbers in blockers_by_number.items():
        for blocker_number in blocker_numbers:
            blocks_by_number[blocker_number].add(blocked_number)

    return children_by_number, blockers_by_number, blocks_by_number


def build_nodes_and_live(
    wanted: set[int],
    expanded: dict[int, ExpandedIssue],
    parent_by_number: dict[int, int | None],
    children_by_number: dict[int, set[int]],
    blockers_by_number: dict[int, set[int]],
    blocks_by_number: dict[int, set[int]],
    project_values: dict[int, dict[str, str]],
    source: GitHubTicketGraphConfig,
    diagnostics: list[TicketDiagnostic],
) -> tuple[list[TicketNode], list[TicketLiveMetadata]]:
    """Build ``TicketNode`` and ``TicketLiveMetadata`` lists for every wanted
    issue, applying status/priority/type defaulting and emitting
    ``METADATA_DEFAULTED`` diagnostics as needed.
    """
    nodes: list[TicketNode] = []
    live: list[TicketLiveMetadata] = []

    for number in sorted(wanted):
        issue = expanded[number]
        title = issue.title
        state = issue.state
        body = issue.body
        metadata = parse_metadata(body, issue.labels)
        overlay = project_values.get(number, {})

        status: TicketStatus
        if state == "CLOSED":
            status = TicketStatus.DONE
        else:
            raw_status = enum_value(
                TicketStatus,
                overlay.get(source.status_field) or metadata.get("status"),
            )
            status = raw_status if raw_status is not None else TicketStatus.BACKLOG

        priority = enum_value(
            TicketPriority,
            overlay.get(source.priority_field) or metadata.get("priority"),
        )
        if priority is None:
            priority = TicketPriority.P3

        ticket_type = enum_value(
            TicketType,
            overlay.get(source.type_field) or metadata.get("type"),
        )
        if ticket_type is None:
            if number == source.root_issue:
                ticket_type = TicketType.PROGRAM
            elif children_by_number.get(number):
                ticket_type = TicketType.INITIATIVE
            else:
                ticket_type = TicketType.IMPLEMENTATION_TICKET

        defaulted_fields: list[str] = []
        if status is TicketStatus.BACKLOG and raw_status is None:
            defaulted_fields.append("status")
        if (
            priority is TicketPriority.P3
            and not overlay.get(source.priority_field)
            and not metadata.get("priority")
        ):
            defaulted_fields.append("priority")

        if defaulted_fields:
            diagnostics.append(
                TicketDiagnostic(
                    "METADATA_DEFAULTED",
                    number,
                    "metadata fields "
                    + ", ".join(defaulted_fields)
                    + " are missing; defaulted for readiness",
                )
            )

        initiative = overlay.get(source.initiative_field) or metadata.get("initiative")
        roadmap = (
            (initiative.strip(),)
            if isinstance(initiative, str) and initiative.strip()
            else ("github",)
        )

        nodes.append(
            TicketNode(
                number=number,
                title=title,
                ticket_type=ticket_type,
                priority=priority,
                status=status,
                parent=parent_by_number.get(number),
                blockers=tuple(sorted(blockers_by_number.get(number, ()))),
                blocks=tuple(sorted(blocks_by_number.get(number, ()))),
                children=tuple(sorted(children_by_number.get(number, ()))),
                roadmap=roadmap,
            )
        )
        live.append(
            TicketLiveMetadata(
                number,
                title,
                state,
                body,
                expanded[number].comments,
            )
        )

    return nodes, live


def build_capability_coverage(
    issue_unavailable: set[int],
    sub_issues_unavailable: set[int],
    sub_issues_truncated: bool,
    comments_unavailable: set[int],
    comments_truncated: bool,
    dependencies_unavailable: set[int],
    dependencies_truncated: bool,
    project_unavailable: set[int],
    project_complete: bool,
) -> tuple[CapabilityCoverage, ...]:
    """Construct the canonical five-capability coverage tuple."""
    return (
        CapabilityCoverage(
            GraphEvidenceCapability.ISSUE,
            not issue_unavailable,
            tuple(sorted(issue_unavailable)),
            False,
        ),
        CapabilityCoverage(
            GraphEvidenceCapability.SUB_ISSUES,
            not sub_issues_unavailable and not sub_issues_truncated,
            tuple(sorted(sub_issues_unavailable)),
            sub_issues_truncated,
        ),
        CapabilityCoverage(
            GraphEvidenceCapability.COMMENTS,
            not comments_unavailable and not comments_truncated,
            tuple(sorted(comments_unavailable)),
            comments_truncated,
        ),
        CapabilityCoverage(
            GraphEvidenceCapability.DEPENDENCIES,
            not dependencies_unavailable and not dependencies_truncated,
            tuple(sorted(dependencies_unavailable)),
            dependencies_truncated,
        ),
        CapabilityCoverage(
            GraphEvidenceCapability.PROJECT_OVERLAY,
            project_complete,
            tuple(sorted(project_unavailable)),
            False,
        ),
    )


__all__ = [
    "build_adjacency_maps",
    "build_capability_coverage",
    "build_nodes_and_live",
]
