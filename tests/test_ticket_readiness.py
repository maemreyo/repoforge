from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from repoforge.application.tickets.live import ticket_live_state_from_issue
from repoforge.application.tickets.readiness import derive_ticket_readiness
from repoforge.domain.tickets import (
    TicketDeliveryMetadata,
    TicketGraph,
    TicketLiveState,
    TicketNode,
    TicketPriority,
    TicketReadinessPolicy,
    TicketStatus,
    TicketType,
)


def _node(
    number: int,
    *,
    ticket_type: TicketType = TicketType.IMPLEMENTATION_TICKET,
    priority: TicketPriority = TicketPriority.P1,
    status: TicketStatus = TicketStatus.BLOCKED,
    parent: int | None = 3,
    blockers: tuple[int, ...] = (),
    blocks: tuple[int, ...] = (),
    children: tuple[int, ...] = (),
) -> TicketNode:
    return TicketNode(
        number=number,
        title=f"#{number}",
        ticket_type=ticket_type,
        priority=priority,
        status=status,
        parent=parent,
        blockers=blockers,
        blocks=blocks,
        children=children,
        roadmap=("master",),
    )


def _live(
    number: int,
    *,
    is_open: bool | None = True,
    complete: bool = True,
    design_gate: bool = False,
    superseded_by: int | None = None,
    wave: int = 0,
    sequence: int = 0,
) -> TicketLiveState:
    return TicketLiveState(
        number=number,
        is_open=is_open,
        delivery=TicketDeliveryMetadata(
            specification_complete=complete,
            unresolved_design_gate=design_gate,
            superseded_by=superseded_by,
            wave=wave,
            sequence=sequence,
        ),
    )


def _graph(*tickets: TicketNode) -> TicketGraph:
    children = tuple(sorted(ticket.number for ticket in tickets if ticket.parent == 3))
    program = _node(
        3,
        ticket_type=TicketType.PROGRAM,
        priority=TicketPriority.P0,
        status=TicketStatus.IN_PROGRESS,
        parent=None,
        children=children,
    )
    return TicketGraph(1, 3, (program, *tickets))


def _by_number(report):
    return {assessment.number: assessment for assessment in report.assessments}


def test_closed_blockers_make_a_complete_ticket_ready() -> None:
    blocker_7 = _node(7, status=TicketStatus.DONE, blocks=(10,))
    blocker_9 = _node(9, status=TicketStatus.DONE, blocks=(10,))
    ticket_10 = _node(10, blockers=(7, 9))
    report = derive_ticket_readiness(
        _graph(blocker_7, blocker_9, ticket_10),
        (_live(3), _live(7, is_open=False), _live(9, is_open=False), _live(10)),
    )

    ticket = _by_number(report)[10]
    assert ticket.derived_status is TicketStatus.READY
    assert ticket.unresolved_blockers == ()
    assert ticket.selectable is True
    assert report.recommended == (10,)
    assert "status: Blocked -> Ready" in ticket.metadata_repairs


def test_open_blocker_inactive_parent_missing_spec_and_design_gate_are_explicit() -> None:
    initiative = _node(
        8,
        ticket_type=TicketType.INITIATIVE,
        status=TicketStatus.BACKLOG,
        children=(10, 11, 12),
    )
    blocker = _node(7, status=TicketStatus.IN_PROGRESS, blocks=(10,))
    blocked = _node(10, parent=8, blockers=(7,))
    incomplete = _node(11, parent=8)
    gated = _node(12, parent=8)
    graph = TicketGraph(
        1,
        3,
        (
            _node(
                3,
                ticket_type=TicketType.PROGRAM,
                priority=TicketPriority.P0,
                status=TicketStatus.IN_PROGRESS,
                parent=None,
                children=(7, 8),
            ),
            blocker,
            initiative,
            blocked,
            incomplete,
            gated,
        ),
    )
    report = derive_ticket_readiness(
        graph,
        (
            _live(3),
            _live(7),
            _live(8),
            _live(10),
            _live(11, complete=False),
            _live(12, design_gate=True),
        ),
    )
    assessments = _by_number(report)

    assert assessments[10].derived_status is TicketStatus.BLOCKED
    assert assessments[10].unresolved_blockers == (7,)
    assert "PARENT_INACTIVE" in assessments[10].reason_codes
    assert "OPEN_BLOCKERS" in assessments[10].reason_codes
    assert "SPECIFICATION_INCOMPLETE" in assessments[11].reason_codes
    assert "PARENT_INACTIVE" in assessments[11].reason_codes
    assert "DESIGN_GATE_UNRESOLVED" in assessments[12].reason_codes
    assert report.recommended == ()


def test_wip_limits_apply_by_priority_and_initiative() -> None:
    initiative = _node(
        8,
        ticket_type=TicketType.INITIATIVE,
        status=TicketStatus.IN_PROGRESS,
        children=(9, 10, 11),
    )
    active = _node(9, parent=8, status=TicketStatus.IN_PROGRESS)
    same_priority = _node(10, parent=8, priority=TicketPriority.P1)
    different_priority = _node(11, parent=8, priority=TicketPriority.P0)
    graph = TicketGraph(
        1,
        3,
        (
            _node(
                3,
                ticket_type=TicketType.PROGRAM,
                priority=TicketPriority.P0,
                status=TicketStatus.IN_PROGRESS,
                parent=None,
                children=(8,),
            ),
            initiative,
            active,
            same_priority,
            different_priority,
        ),
    )
    report = derive_ticket_readiness(
        graph,
        (_live(3), _live(8), _live(9), _live(10), _live(11)),
        policy=TicketReadinessPolicy(
            p0_limit=2,
            p1_limit=1,
            p2_limit=2,
            p3_limit=2,
            initiative_limit=1,
        ),
    )
    assessments = _by_number(report)

    assert "PRIORITY_WIP_LIMIT" in assessments[10].reason_codes
    assert "INITIATIVE_WIP_LIMIT" in assessments[10].reason_codes
    assert "INITIATIVE_WIP_LIMIT" in assessments[11].reason_codes
    assert assessments[10].wip_conflicts == (9,)
    assert assessments[11].wip_conflicts == (9,)
    assert report.recommended == ()


def test_order_is_priority_then_wave_sequence_and_issue_number() -> None:
    tickets = (
        _node(20, priority=TicketPriority.P1),
        _node(21, priority=TicketPriority.P0),
        _node(22, priority=TicketPriority.P0),
        _node(23, priority=TicketPriority.P0),
    )
    report = derive_ticket_readiness(
        _graph(*tickets),
        (
            _live(3),
            _live(20, wave=0, sequence=0),
            _live(21, wave=2, sequence=0),
            _live(22, wave=1, sequence=2),
            _live(23, wave=1, sequence=1),
        ),
        policy=TicketReadinessPolicy.unbounded(),
    )

    assert report.recommended == (23, 22, 21, 20)


def test_closed_stale_status_supersession_and_unreadable_live_state_fail_closed() -> None:
    closed = _node(30, status=TicketStatus.READY)
    superseded = _node(31, status=TicketStatus.READY)
    unreadable = _node(32, status=TicketStatus.READY)
    report = derive_ticket_readiness(
        _graph(closed, superseded, unreadable),
        (
            _live(3),
            _live(30, is_open=False),
            _live(31, superseded_by=40),
            _live(32, is_open=None),
        ),
    )
    assessments = _by_number(report)

    assert assessments[30].derived_status is TicketStatus.DONE
    assert assessments[30].metadata_repairs == ("status: Ready -> Done",)
    assert assessments[31].derived_status is TicketStatus.SUPERSEDED
    assert assessments[31].reason_codes == ("SUPERSEDED",)
    assert assessments[32].derived_status is TicketStatus.BLOCKED
    assert assessments[32].reason_codes == ("LIVE_METADATA_UNAVAILABLE",)
    assert report.recommended == ()


def test_cycles_asymmetry_and_partial_graph_return_diagnostics_not_selection() -> None:
    first = _node(10, blockers=(11,), blocks=(11,))
    second = _node(11, blockers=(10,), blocks=(10,))
    report = derive_ticket_readiness(
        _graph(first, second),
        (_live(3), _live(10), _live(11)),
    )
    assert report.recommended == ()
    assert {item.code for item in report.diagnostics} == {"CIRCULAR_DEPENDENCY"}

    asymmetric = _node(20, blockers=(21,))
    missing = _node(22, blockers=(999,))
    invalid = derive_ticket_readiness(
        _graph(asymmetric, _node(21), missing),
        (_live(3), _live(20), _live(21), _live(22)),
    )
    assert invalid.recommended == ()
    assert {item.code for item in invalid.diagnostics} >= {
        "ASYMMETRIC_BLOCKS",
        "UNKNOWN_BLOCKER",
    }


def test_repeated_derivation_is_immutable_and_deterministic() -> None:
    graph = _graph(_node(10), _node(11, priority=TicketPriority.P0))
    live = (_live(3), _live(10), _live(11))

    first = derive_ticket_readiness(graph, live, policy=TicketReadinessPolicy.unbounded())
    second = derive_ticket_readiness(graph, live, policy=TicketReadinessPolicy.unbounded())

    assert first == second
    assert graph.nodes[1].status is TicketStatus.BLOCKED


def test_live_issue_metadata_normalizes_spec_gate_supersession_and_order() -> None:
    state = ticket_live_state_from_issue(
        {
            "number": 68,
            "state": "OPEN",
            "body": "Delivery wave: 2\nSequence: 7\nDesign gate: unresolved",
            "comments": [
                {
                    "body": (
                        "Objective: derive readiness.\n"
                        "Acceptance criteria: closed blockers become ready.\n"
                        "Tests: cover deterministic ordering.\n"
                        "Superseded by: #70"
                    )
                }
            ],
        },
        expected_number=68,
    )

    assert state.is_open is True
    assert state.delivery.specification_complete is True
    assert state.delivery.unresolved_design_gate is True
    assert state.delivery.superseded_by == 70
    assert state.delivery.wave == 2
    assert state.delivery.sequence == 7


def test_concurrent_derivation_is_deterministic() -> None:
    graph = _graph(_node(10), _node(11, priority=TicketPriority.P0))
    live = (_live(3), _live(10), _live(11))

    with ThreadPoolExecutor(max_workers=8) as pool:
        reports = tuple(
            pool.map(
                lambda _index: derive_ticket_readiness(
                    graph,
                    live,
                    policy=TicketReadinessPolicy.unbounded(),
                ),
                range(32),
            )
        )

    assert all(report == reports[0] for report in reports)
