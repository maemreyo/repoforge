from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from repoforge.adapters.github.ticket_graph import GitHubTicketGraphReader
from repoforge.application.tickets.graph import (
    compare_live_ticket_metadata,
    load_ticket_graph,
    select_ready_tickets,
    validate_ticket_graph,
)
from repoforge.domain.tickets import TicketGraphError, TicketLiveMetadata
from repoforge.ports.command import CommandResult


def _write_graph(tmp_path: Path, nodes: list[dict[str, object]]) -> Path:
    path = tmp_path / "graph.json"
    path.write_text(
        json.dumps({"schema_version": 1, "program_issue": 3, "nodes": nodes}),
        encoding="utf-8",
    )
    return path


def _node(
    number: int,
    *,
    ticket_type: str = "implementation_ticket",
    priority: str = "P0",
    status: str = "Ready",
    parent: int | None = 3,
    blockers: list[int] | None = None,
    blocks: list[int] | None = None,
    children: list[int] | None = None,
) -> dict[str, object]:
    return {
        "number": number,
        "title": f"Issue {number}",
        "type": ticket_type,
        "priority": priority,
        "status": status,
        "parent": parent,
        "blockers": blockers or [],
        "blocks": blocks or [],
        "children": children or [],
        "roadmap": ["Roadmap section"],
    }


class RecordingExecutor:
    def __init__(self, responses: dict[int, dict[str, object]]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, ...]] = []

    def environment(self, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        return dict(extra or {})

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        input_text: str | None = None,
        timeout: int | None = None,
        check: bool = True,
        extra_env: Mapping[str, str] | None = None,
        output_limit: int | None = None,
    ) -> CommandResult:
        del input_text, timeout, check, extra_env, output_limit
        command = tuple(argv)
        self.calls.append(command)
        issue_number = int(command[3])
        return CommandResult(command, str(cwd), 0, json.dumps(self.responses[issue_number]), "")

    def run_bytes(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        timeout: int | None = None,
        max_bytes: int,
    ) -> bytes:
        del argv, cwd, timeout, max_bytes
        raise AssertionError("live ticket reads must not use binary command execution")


def test_ticket_graph_validates_and_selects_by_priority_then_number(tmp_path: Path) -> None:
    program = _node(
        3,
        ticket_type="program",
        status="In progress",
        parent=None,
        children=[10, 11, 12],
    )
    graph = load_ticket_graph(
        _write_graph(
            tmp_path,
            [program, _node(10, priority="P1"), _node(12), _node(11)],
        )
    )

    assert validate_ticket_graph(graph) == ()
    assert [item.number for item in select_ready_tickets(graph, limit=10)] == [11, 12, 10]


@pytest.mark.parametrize(
    ("nodes", "code"),
    [
        (
            [
                _node(3, ticket_type="program", status="In progress", parent=None),
                _node(3),
            ],
            "DUPLICATE_ID",
        ),
        (
            [
                _node(3, ticket_type="program", status="In progress", parent=None),
                _node(4, parent=99),
            ],
            "MISSING_PARENT",
        ),
        (
            [
                _node(3, ticket_type="program", status="In progress", parent=None),
                _node(4, blockers=[99]),
            ],
            "UNKNOWN_BLOCKER",
        ),
        (
            [
                _node(3, ticket_type="program", status="In progress", parent=None),
                _node(4, blockers=[4], blocks=[4]),
            ],
            "CIRCULAR_DEPENDENCY",
        ),
    ],
)
def test_ticket_graph_rejects_invalid_structure(
    tmp_path: Path,
    nodes: list[dict[str, object]],
    code: str,
) -> None:
    graph = load_ticket_graph(_write_graph(tmp_path, nodes))
    assert code in {item.code for item in validate_ticket_graph(graph)}


def test_ticket_graph_detects_asymmetry_ready_blocker_and_child_drift(tmp_path: Path) -> None:
    program = _node(
        3,
        ticket_type="program",
        status="In progress",
        parent=None,
        children=[4],
    )
    blocker = _node(4, status="In progress")
    ready = _node(5, blockers=[4])
    graph = load_ticket_graph(_write_graph(tmp_path, [program, blocker, ready]))
    codes = {item.code for item in validate_ticket_graph(graph)}
    assert {"ASYMMETRIC_BLOCKS", "READY_WITH_OPEN_BLOCKER", "PARENT_CHILD_DRIFT"}.issubset(codes)
    assert select_ready_tickets(graph, limit=10) == ()


def test_ticket_graph_excludes_backlog_and_bounds_limit(tmp_path: Path) -> None:
    program = _node(
        3,
        ticket_type="program",
        status="In progress",
        parent=None,
        children=[4, 5],
    )
    graph = load_ticket_graph(
        _write_graph(tmp_path, [program, _node(4, status="Backlog"), _node(5)])
    )
    assert [item.number for item in select_ready_tickets(graph, limit=1)] == [5]
    with pytest.raises(TicketGraphError):
        select_ready_tickets(graph, limit=0)


def test_checked_in_ticket_graph_and_issue_forms_are_complete() -> None:
    root = Path(__file__).parents[1]
    graph = load_ticket_graph(root / "docs/roadmaps/REPOFORGE_TICKET_GRAPH.json")
    assert graph.program_issue == 3
    assert len(graph.nodes) >= 80
    assert validate_ticket_graph(graph) == ()

    required = {
        "Type",
        "Priority",
        "Status",
        "Parent",
        "Blocked by",
        "Blocks",
        "Roadmap",
        "Objective",
        "User value",
        "Scope",
        "Non-goals",
        "Architecture / contracts",
        "Acceptance criteria",
        "Tests",
        "Final verification",
        "Migration / compatibility",
        "Expected PR shape",
    }
    for name in ("initiative.yml", "implementation-ticket.yml"):
        text = (root / ".github/ISSUE_TEMPLATE" / name).read_text(encoding="utf-8")
        assert all(field in text for field in required)
        assert "Ready" in text
        assert "required: true" in text


def test_live_reader_is_bounded_read_only_and_normalizes_metadata(tmp_path: Path) -> None:
    body = "\n".join(
        (
            "**Type:** implementation_ticket",
            "**Priority:** P0",
            "**Status:** Ready",
            "**Parent:** #3",
        )
    )
    executor = RecordingExecutor(
        {4: {"number": 4, "title": "Issue 4", "state": "OPEN", "body": body}}
    )
    snapshots = GitHubTicketGraphReader(executor, cwd=tmp_path).read("owner/repo", (4,))
    assert snapshots == (TicketLiveMetadata(4, "Issue 4", "OPEN", body),)
    assert executor.calls == [
        (
            "gh",
            "issue",
            "view",
            "4",
            "--repo",
            "owner/repo",
            "--json",
            "number,title,state,body",
        )
    ]
    assert not {"create", "edit", "close", "delete", "comment"}.intersection(executor.calls[0])


def test_live_drift_reports_title_state_and_body_mismatches(tmp_path: Path) -> None:
    program = _node(
        3,
        ticket_type="program",
        status="In progress",
        parent=None,
        children=[4],
    )
    graph = load_ticket_graph(_write_graph(tmp_path, [program, _node(4)]))
    live = (
        TicketLiveMetadata(
            3,
            "Issue 3",
            "OPEN",
            "**Type:** program\n**Priority:** P0\n**Status:** In progress\n**Parent:** None",
        ),
        TicketLiveMetadata(
            4,
            "Different title",
            "CLOSED",
            "**Type:** implementation_ticket\n**Priority:** P1\n**Status:** Backlog\n**Parent:** #3",
        ),
    )
    codes = {item.code for item in compare_live_ticket_metadata(graph, live)}
    assert {"LIVE_TITLE_DRIFT", "LIVE_STATE_DRIFT", "LIVE_BODY_DRIFT"}.issubset(codes)
