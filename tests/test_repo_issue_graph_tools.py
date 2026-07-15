"""CodingService-level tests for repo_issue_graph/repo_issue_next/repo_issue_spec (#64)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import create_forge_environment

from repoforge.application.service import CodingService
from repoforge.config import load_config
from repoforge.domain.tickets import TicketGraphError


def _write_manifest(
    source: Path, nodes: list[dict[str, object]], *, program_issue: int = 3
) -> None:
    manifest_dir = source / "docs" / "roadmaps"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    (manifest_dir / "REPOFORGE_TICKET_GRAPH.json").write_text(
        json.dumps({"schema_version": 1, "program_issue": program_issue, "nodes": nodes}),
        encoding="utf-8",
    )


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
        "title": f"#{number}",
        "type": ticket_type,
        "priority": priority,
        "status": status,
        "parent": parent,
        "blockers": blockers or [],
        "blocks": blocks or [],
        "children": children or [],
        "roadmap": ["master"],
    }


def _service(tmp_path: Path):
    environment = create_forge_environment(tmp_path)
    return CodingService(load_config(environment.config_path)), environment


def test_repo_issue_graph_reports_no_manifest_when_absent(tmp_path: Path) -> None:
    service, _ = _service(tmp_path)
    result = service.repo_issue_graph("demo")
    assert result == {
        "repo_id": "demo",
        "manifest_found": False,
        "program_issue": None,
        "nodes": [],
        "node_count": 0,
        "truncated": False,
    }


def test_repo_issue_graph_filters_by_status_priority_and_initiative(tmp_path: Path) -> None:
    service, environment = _service(tmp_path)
    program = _node(3, ticket_type="program", status="In progress", parent=None, children=[8, 20])
    initiative_a = _node(8, ticket_type="initiative", status="In progress", children=[9, 10])
    ticket_a1 = _node(9, parent=8, priority="P0", status="Ready")
    ticket_a2 = _node(10, parent=8, priority="P1", status="Blocked")
    initiative_b = _node(20, ticket_type="initiative", status="In progress", children=[21])
    ticket_b1 = _node(21, parent=20, priority="P0", status="Ready")
    _write_manifest(
        environment.source,
        [program, initiative_a, ticket_a1, ticket_a2, initiative_b, ticket_b1],
    )

    all_nodes = service.repo_issue_graph("demo")
    assert all_nodes["manifest_found"] is True
    assert all_nodes["program_issue"] == 3
    assert all_nodes["node_count"] == 6

    by_initiative = service.repo_issue_graph("demo", initiative=8)
    assert {item["number"] for item in by_initiative["nodes"]} == {8, 9, 10}

    by_status = service.repo_issue_graph("demo", status="Ready")
    assert {item["number"] for item in by_status["nodes"]} == {9, 21}

    by_priority = service.repo_issue_graph("demo", priority="P1")
    assert {item["number"] for item in by_priority["nodes"]} == {10}

    by_root = service.repo_issue_graph("demo", root_issue=20)
    assert {item["number"] for item in by_root["nodes"]} == {20, 21}


def test_repo_issue_graph_rejects_a_non_initiative_scope(tmp_path: Path) -> None:
    service, environment = _service(tmp_path)
    program = _node(3, ticket_type="program", status="In progress", parent=None, children=[9])
    ticket = _node(9)
    _write_manifest(environment.source, [program, ticket])

    with pytest.raises(TicketGraphError, match="not an initiative"):
        service.repo_issue_graph("demo", initiative=9)


def test_repo_issue_next_reports_diagnostics_for_an_invalid_manifest(tmp_path: Path) -> None:
    service, environment = _service(tmp_path)
    program = _node(3, ticket_type="program", status="In progress", parent=None, children=[9])
    orphan = _node(9, blockers=[999])
    _write_manifest(environment.source, [program, orphan])

    result = service.repo_issue_next("demo")
    assert result["manifest_found"] is True
    assert result["valid"] is False
    assert any(item["code"] == "UNKNOWN_BLOCKER" for item in result["diagnostics"])
    assert result["tickets"] == []


def test_repo_issue_next_selects_by_priority_then_number_within_scope(tmp_path: Path) -> None:
    service, environment = _service(tmp_path)
    program = _node(3, ticket_type="program", status="In progress", parent=None, children=[8, 20])
    initiative_a = _node(8, ticket_type="initiative", status="Ready", children=[10, 11])
    ticket_a1 = _node(11, parent=8, priority="P1")
    ticket_a2 = _node(10, parent=8, priority="P0")
    initiative_b = _node(20, ticket_type="initiative", status="In progress", children=[21])
    ticket_b1 = _node(21, parent=20, priority="P0")
    _write_manifest(
        environment.source,
        [program, initiative_a, ticket_a1, ticket_a2, initiative_b, ticket_b1],
    )

    unscoped = service.repo_issue_next("demo", limit=10)
    assert [item["number"] for item in unscoped["tickets"]] == [10, 21, 11]
    # The Ready initiative #8 itself must never be offered as a pickable ticket.
    assert 8 not in [item["number"] for item in unscoped["tickets"]]

    scoped = service.repo_issue_next("demo", root_issue=8, limit=10)
    assert [item["number"] for item in scoped["tickets"]] == [10, 11]


def test_repo_issue_spec_combines_manifest_node_and_live_issue(tmp_path: Path) -> None:
    service, environment = _service(tmp_path)
    program = _node(3, ticket_type="program", status="In progress", parent=None, children=[9])
    ticket = _node(9, priority="P0", status="Blocked")
    _write_manifest(environment.source, [program, ticket])

    result = service.repo_issue_spec("demo", 9)
    assert result["manifest_found"] is True
    assert result["node"]["number"] == 9
    assert result["live"]["title"] == "Implement safer workflow"
    assert result["live"]["state"] == "OPEN"
    assert result["comments"][0]["body"] == "context"
    assert "heading" in result["comments"][0]


def test_repo_issue_spec_works_without_a_manifest_node(tmp_path: Path) -> None:
    service, _ = _service(tmp_path)
    result = service.repo_issue_spec("demo", 999)
    assert result["manifest_found"] is False
    assert result["node"] is None
    assert result["drift"] == []
    assert result["live"]["title"] == "Implement safer workflow"
