"""CodingService-level tests for repo_issue_graph/repo_issue_next/repo_issue_spec (#64)."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from conftest import create_forge_environment

from repoforge.application.service import CodingService
from repoforge.application.tickets.graph import load_ticket_graph
from repoforge.config import GitHubTicketGraphConfig, load_config
from repoforge.domain.errors import ConfigError
from repoforge.domain.tickets import TicketGraphError, TicketGraphSnapshot, TicketLiveMetadata


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


class FixtureTicketGraphGateway:
    def __init__(self, source: Path, gh_state: Path) -> None:
        self.source = source
        self.gh_state = gh_state
        self.calls = 0

    def read(
        self, cwd: Path, source: GitHubTicketGraphConfig, *, max_items: int
    ) -> TicketGraphSnapshot:
        del cwd, source, max_items
        self.calls += 1
        graph = load_ticket_graph(self.source / "docs" / "roadmaps" / "REPOFORGE_TICKET_GRAPH.json")
        state_payload = (
            json.loads(self.gh_state.read_text(encoding="utf-8"))
            if self.gh_state.is_file()
            else {"issues": {}}
        )
        issue_states = state_payload.get("issues", {})
        live = tuple(
            TicketLiveMetadata(
                node.number,
                node.title,
                str(
                    issue_states.get(str(node.number), {}).get(
                        "state", "CLOSED" if node.status.value == "Done" else "OPEN"
                    )
                ),
                "Objective\nAcceptance criteria\nTests",
            )
            for node in graph.nodes
        )
        return TicketGraphSnapshot(
            graph,
            "2026-07-16T00:00:00+00:00",
            bool(state_payload.get("evidence_complete", True)),
            tuple(int(item) for item in state_payload.get("unavailable", [])),
            bool(state_payload.get("truncated", False)),
            live,
        )


def _service(tmp_path: Path, *, configured: bool = True):
    environment = create_forge_environment(tmp_path)
    _write_manifest(
        environment.source,
        [_node(3, ticket_type="program", status="In progress", parent=None)],
    )
    config = load_config(environment.config_path)
    repo = replace(
        config.repositories["demo"],
        ticket_graph=(
            GitHubTicketGraphConfig(root_issue=3, repository="owner/demo") if configured else None
        ),
    )
    config = replace(config, repositories={"demo": repo})
    gateway = FixtureTicketGraphGateway(environment.source, environment.gh_state)
    return CodingService(config, ticket_graphs=gateway), environment


def _audit_events(root: Path, action: str) -> list[dict[str, object]]:
    audit_path = root / "state" / "audit.jsonl"
    if not audit_path.is_file():
        return []
    events = [
        json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines() if line
    ]
    return [event for event in events if event["action"] == action]


def _audit_events_with_prefix(root: Path, prefix: str) -> list[dict[str, object]]:
    audit_path = root / "state" / "audit.jsonl"
    if not audit_path.is_file():
        return []
    events = [
        json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines() if line
    ]
    return [event for event in events if str(event.get("action", "")).startswith(prefix)]


def test_v2_repo_issue_reports_graph_unavailable_with_next_action(tmp_path: Path) -> None:
    service, environment = _service(tmp_path, configured=False)

    result = service.repo_issue_v2("demo", mode="graph")

    assert result["graph_status"] == "graph_unavailable"
    assert result["nodes"] == []
    assert result["selected"] == []
    assert result["next_action"]
    assert "configure" in result["next_action"].lower()
    assert len(_audit_events(environment.root, "repo_issue")) == 1
    assert _audit_events(environment.root, "repo_issue_graph") == []


def test_repo_issue_graph_reports_missing_configuration_as_invalid(tmp_path: Path) -> None:
    service, _ = _service(tmp_path, configured=False)

    result = service.repo_issue_graph("demo")

    assert result["valid"] is False
    assert result["nodes"] == []
    assert result["diagnostics"] == [
        {
            "code": "GRAPH_NOT_CONFIGURED",
            "issue_number": 0,
            "message": "Configure repositories.demo.ticket_graph.root_issue",
        }
    ]
    assert result["coverage"] == {
        "configured_root": None,
        "observed_root": None,
        "observed_nodes": 0,
        "unavailable": [],
        "truncated": False,
        "evidence_complete": False,
    }
    assert "rf repo refresh demo" in result["safe_next_action"]


def test_repo_issue_graph_uses_github_snapshot_without_production_manifest(tmp_path: Path) -> None:
    service, _ = _service(tmp_path)
    result = service.repo_issue_graph("demo")
    assert result["source"] == "github"
    assert result["program_issue"] == 3
    assert result["node_count"] == 1
    assert result["evidence_complete"] is True
    assert result["truncated"] is False


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
    assert all_nodes["source"] == "github"
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


def test_repo_issue_next_fails_closed_when_graph_evidence_is_incomplete(tmp_path: Path) -> None:
    service, environment = _service(tmp_path)
    program = _node(3, ticket_type="program", status="In progress", parent=None, children=[9])
    ready = _node(9, status="Ready")
    _write_manifest(environment.source, [program, ready])
    environment.gh_state.write_text(
        json.dumps(
            {
                "issues": {"3": {"state": "OPEN"}, "9": {"state": "OPEN"}},
                "evidence_complete": False,
                "unavailable": [9],
            }
        ),
        encoding="utf-8",
    )

    result = service.repo_issue_next("demo", limit=10)

    assert result["valid"] is False
    assert result["tickets"] == []
    assert result["assessments"] == []
    assert result["diagnostics"] == [
        {
            "code": "GRAPH_EVIDENCE_INCOMPLETE",
            "issue_number": 3,
            "message": "GitHub ticket graph evidence is incomplete; unavailable issues: 9",
        }
    ]


def test_repo_issue_next_reports_diagnostics_for_an_invalid_manifest(tmp_path: Path) -> None:
    service, environment = _service(tmp_path)
    program = _node(3, ticket_type="program", status="In progress", parent=None, children=[9])
    orphan = _node(9, blockers=[999])
    _write_manifest(environment.source, [program, orphan])

    result = service.repo_issue_next("demo")
    assert result["source"] == "github"
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


def test_repo_issue_next_derives_closed_blockers_and_metadata_repairs(tmp_path: Path) -> None:
    service, environment = _service(tmp_path)
    program = _node(
        3,
        ticket_type="program",
        status="In progress",
        parent=None,
        children=[7, 9, 10],
    )
    blocker_7 = _node(7, status="Done", blocks=[10])
    blocker_9 = _node(9, status="Done", blocks=[10])
    ticket = _node(10, status="Blocked", blockers=[7, 9])
    _write_manifest(environment.source, [program, blocker_7, blocker_9, ticket])
    environment.gh_state.write_text(
        json.dumps(
            {
                "prs": {},
                "issues": {
                    "3": {"state": "OPEN"},
                    "7": {"state": "CLOSED"},
                    "9": {"state": "CLOSED"},
                    "10": {"state": "OPEN"},
                },
            }
        ),
        encoding="utf-8",
    )

    result = service.repo_issue_next("demo", limit=10)

    assert result["valid"] is True
    assert [item["number"] for item in result["tickets"]] == [10]
    readiness = result["tickets"][0]["readiness"]
    assert readiness["derived_status"] == "Ready"
    assert readiness["unresolved_blockers"] == []
    assert result["metadata_repairs"] == [
        {"issue_number": 10, "repairs": ["status: Blocked -> Ready"]}
    ]


def test_repo_issue_spec_combines_manifest_node_and_live_issue(tmp_path: Path) -> None:
    service, environment = _service(tmp_path)
    program = _node(3, ticket_type="program", status="In progress", parent=None, children=[9])
    ticket = _node(9, priority="P0", status="Blocked")
    _write_manifest(environment.source, [program, ticket])

    result = service.repo_issue_spec("demo", 9)
    assert result["graph_member"] is True
    assert result["node"]["number"] == 9
    assert result["live"]["title"] == "Implement safer workflow"
    assert result["live"]["state"] == "OPEN"
    assert result["comments"][0]["body"].startswith("context")
    assert "heading" in result["comments"][0]


def test_repo_issue_spec_reports_live_spec_drift_without_a_graph_node(tmp_path: Path) -> None:
    service, environment = _service(tmp_path)
    environment.gh_state.write_text(
        json.dumps({"issues": {"999": {"body": "Issue body", "comments": []}}}),
        encoding="utf-8",
    )
    result = service.repo_issue_spec("demo", 999, fresh=True)
    assert result["graph_member"] is False
    assert result["node"] is None
    assert result["drift"] == [
        {
            "code": "LIVE_SPEC_INCOMPLETE",
            "message": "live issue is missing objective, acceptance, or verification evidence",
        }
    ]
    assert result["live"]["title"] == "Implement safer workflow"


def test_repo_issue_graph_produces_exactly_one_bounded_audit_event(tmp_path: Path) -> None:
    service, environment = _service(tmp_path)
    program = _node(3, ticket_type="program", status="In progress", parent=None, children=[9])
    ticket = _node(9)
    _write_manifest(environment.source, [program, ticket])

    service.repo_issue_graph("demo", status="Ready")

    events = _audit_events(environment.root, "repo_issue_graph")
    assert len(events) == 1
    event = events[0]
    assert event["success"] is True
    details = event["details"]
    assert details["repo_id"] == "demo"
    assert details["status"] == "Ready"
    assert details["node_count"] == 1
    assert details["source"] == "github"
    # Bounded: no ticket titles or bodies, only identifiers/filters and counts.
    assert set(details) == {
        "repo_id",
        "root_issue",
        "status",
        "priority",
        "initiative",
        "fresh",
        "source",
        "cache_hit",
        "node_count",
        "truncated",
        "evidence_complete",
        "correlation_id",
        "duration_ms",
        "result_bytes",
        "is_mutating",
    }
    assert "title" not in json.dumps(details)
    assert "#9" not in json.dumps(details)


def test_repo_issue_graph_audits_failure_for_an_invalid_initiative(tmp_path: Path) -> None:
    service, environment = _service(tmp_path)
    program = _node(3, ticket_type="program", status="In progress", parent=None, children=[9])
    ticket = _node(9)
    _write_manifest(environment.source, [program, ticket])

    with pytest.raises(TicketGraphError, match="not an initiative"):
        service.repo_issue_graph("demo", initiative=9)

    events = _audit_events(environment.root, "repo_issue_graph")
    assert len(events) == 1
    event = events[0]
    assert event["success"] is False
    assert event["details"]["initiative"] == 9
    assert event["details"]["error_type"] == "TicketGraphError"
    # No ticket titles or graph payloads leak into the failure audit details.
    assert "title" not in json.dumps(event["details"])


def test_repo_issue_next_produces_exactly_one_bounded_audit_event(tmp_path: Path) -> None:
    service, environment = _service(tmp_path)
    program = _node(3, ticket_type="program", status="In progress", parent=None, children=[9])
    ready = _node(9, priority="P0", status="Ready")
    _write_manifest(environment.source, [program, ready])

    result = service.repo_issue_next("demo", limit=5)
    assert result["tickets"][0]["number"] == 9

    events = _audit_events(environment.root, "repo_issue_next")
    assert len(events) == 1
    event = events[0]
    assert event["success"] is True
    details = event["details"]
    assert details["repo_id"] == "demo"
    assert details["limit"] == 5
    assert details["ticket_count"] == 1
    assert details["source"] == "github"
    assert details["valid"] is True
    # Bounded: no ticket titles or bodies in the audit trail.
    assert set(details) == {
        "repo_id",
        "root_issue",
        "limit",
        "fresh",
        "source",
        "cache_hit",
        "evidence_complete",
        "valid",
        "ticket_count",
        "correlation_id",
        "duration_ms",
        "result_bytes",
        "is_mutating",
    }
    assert "title" not in json.dumps(details)

    all_issue_next_events = _audit_events_with_prefix(environment.root, "repo_issue_next")
    assert [event["action"] for event in all_issue_next_events] == ["repo_issue_next"]


def test_repo_issue_next_audits_failure_for_an_out_of_range_limit(tmp_path: Path) -> None:
    service, environment = _service(tmp_path)
    program = _node(3, ticket_type="program", status="In progress", parent=None, children=[9])
    ready = _node(9, priority="P0", status="Ready")
    _write_manifest(environment.source, [program, ready])

    with pytest.raises(TicketGraphError, match="limit must be between"):
        service.repo_issue_next("demo", limit=0)

    events = _audit_events(environment.root, "repo_issue_next")
    assert len(events) == 1
    event = events[0]
    assert event["success"] is False
    assert event["details"]["limit"] == 0
    assert event["details"]["error_type"] == "TicketGraphError"


def test_repo_issue_next_audits_unknown_repository_failure(tmp_path: Path) -> None:
    service, environment = _service(tmp_path)

    with pytest.raises(ConfigError, match="Unknown repository id"):
        service.repo_issue_next("missing", limit=1)

    events = _audit_events_with_prefix(environment.root, "repo_issue_next")
    assert len(events) == 1
    assert events[0]["action"] == "repo_issue_next"
    assert events[0]["success"] is False
    assert events[0]["details"]["repo_id"] == "missing"
    assert events[0]["details"]["error_type"] == "ConfigError"
