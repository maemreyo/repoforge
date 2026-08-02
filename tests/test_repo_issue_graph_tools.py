"""CodingService-level tests for repo_issue_graph/repo_issue_next/repo_issue_spec (#64)."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest
from conftest import create_forge_environment, execution_coordinator_for_tests
from mcp.shared.memory import create_connected_server_and_client_session

from repoforge.adapters.audit import JsonlAuditSink
from repoforge.adapters.persistence import JsonGitHubReadCache
from repoforge.application.context import ApplicationContext
from repoforge.application.repository.doctor import Doctor, DoctorCommand
from repoforge.application.repository.issue_graph import (
    _observed_age_ms,
    _snapshot_payload,
    read_github_ticket_snapshot,
)
from repoforge.application.service import CodingService
from repoforge.application.tickets.graph import load_ticket_graph
from repoforge.config import (
    AppConfig,
    GitHubTicketGraphConfig,
    RepositoryConfig,
    ServerConfig,
    load_config,
)
from repoforge.contracts.registry import V2_TOOL_SPECS
from repoforge.domain.errors import ConfigError
from repoforge.domain.tickets import (
    CapabilityCoverage,
    CapabilityReadStat,
    GraphEvidenceCapability,
    TicketDiagnostic,
    TicketGraph,
    TicketGraphError,
    TicketGraphReadStats,
    TicketGraphSnapshot,
    TicketLiveMetadata,
    TicketNode,
    TicketPriority,
    TicketStatus,
    TicketType,
)
from repoforge.interfaces.mcp.server import create_server
from repoforge.testing import (
    FixedClock,
    InMemoryLockManager,
    InMemoryOperationGate,
    InMemoryWorkspaceStore,
    SequenceIdGenerator,
)


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
        self.read_stats: TicketGraphReadStats | None = None

    def read(
        self, cwd: Path, source: GitHubTicketGraphConfig, *, max_items: int, remote: str = "origin"
    ) -> TicketGraphSnapshot:
        del cwd, source, max_items, remote
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
                tuple(
                    str(item.get("body"))
                    for item in issue_states.get(str(node.number), {}).get("comments", [])
                    if isinstance(item, dict) and isinstance(item.get("body"), str)
                ),
            )
            for node in graph.nodes
        )
        capability_coverage = tuple(
            CapabilityCoverage(
                GraphEvidenceCapability(str(item["capability"])),
                bool(item["complete"]),
                tuple(int(number) for number in item.get("unavailable", [])),
                bool(item.get("truncated", False)),
            )
            for item in state_payload.get("capability_coverage", [])
        )
        diagnostics = tuple(
            TicketDiagnostic(
                code=str(item["code"]),
                issue_number=int(item["issue_number"]),
                message=str(item.get("message", "")),
            )
            for item in state_payload.get("diagnostics", [])
            if isinstance(item, dict) and isinstance(item.get("issue_number"), int)
        )
        return TicketGraphSnapshot(
            graph,
            "2026-07-16T00:00:00+00:00",
            bool(state_payload.get("evidence_complete", True)),
            tuple(int(item) for item in state_payload.get("unavailable", [])),
            bool(state_payload.get("truncated", False)),
            live,
            capability_coverage,
            read_stats=self.read_stats,
            diagnostics=diagnostics,
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


def test_v2_repo_issue_reports_typed_graph_reason(tmp_path: Path) -> None:
    service, environment = _service(tmp_path, configured=False)

    result = service.repo_issue_v2("demo", mode="graph")

    V2_TOOL_SPECS["repo_issue"].validate_output(result)
    assert result["graph_status"] == "graph_unavailable"
    assert result["graph_unavailable_reason"] == "configuration_unavailable"
    assert result["nodes"] == []
    assert result["selected"] == []
    assert result["next_action"]
    assert "configure" in result["next_action"].lower()
    assert len(_audit_events(environment.root, "repo_issue")) == 1
    assert _audit_events(environment.root, "repo_issue_graph") == []


def test_v2_repo_issue_reports_typed_provider_reason(tmp_path: Path) -> None:
    configured, _environment = _service(tmp_path)

    class UnavailableTicketGraphGateway:
        def read(
            self,
            cwd: Path,
            source: GitHubTicketGraphConfig,
            *,
            max_items: int,
            remote: str = "origin",
        ) -> TicketGraphSnapshot:
            del cwd, source, max_items, remote
            raise ConfigError("GitHub ticket graph transport is offline")

    service = CodingService(
        configured.config,
        ticket_graphs=UnavailableTicketGraphGateway(),
    )
    result = service.repo_issue_v2("demo", mode="graph", fresh=True)

    V2_TOOL_SPECS["repo_issue"].validate_output(result)
    assert result["graph_status"] == "graph_unavailable"
    assert result["graph_unavailable_reason"] == "provider_unavailable"
    assert result["nodes"] == []
    assert "provider" in result["next_action"].lower()
    assert "configure" not in result["next_action"].lower()


def test_doctor_reports_missing_active_graph_projection(tmp_path: Path) -> None:
    service, _environment = _service(tmp_path)
    context = replace(service.application.context, ticket_graphs=None)

    result = Doctor(context).execute(DoctorCommand())

    check = next(item for item in result.checks if item["name"] == "ticket_graph_projection:demo")
    assert check["ok"] is False
    assert check["severity"] == "error"
    assert "no runtime adapter" in check["detail"]
    assert "do not edit" in check["remediation"].lower()


def test_v2_repo_issue_graph_exposes_capability_scoped_coverage(tmp_path: Path) -> None:
    service, environment = _service(tmp_path)
    program = _node(3, ticket_type="program", status="In progress", parent=None, children=[9])
    ticket = _node(9)
    _write_manifest(environment.source, [program, ticket])
    environment.gh_state.write_text(
        json.dumps(
            {
                "issues": {},
                "capability_coverage": [
                    {"capability": "issue", "complete": True, "unavailable": []},
                    {"capability": "sub_issues", "complete": True, "unavailable": []},
                    {"capability": "comments", "complete": False, "unavailable": [9]},
                    {"capability": "dependencies", "complete": True, "unavailable": []},
                    {"capability": "project_overlay", "complete": True, "unavailable": []},
                ],
            }
        ),
        encoding="utf-8",
    )

    result = service.repo_issue_v2("demo", mode="graph", fresh=True)

    coverage = {item["capability"]: item for item in result["capability_coverage"]}
    assert coverage["comments"]["complete"] is False
    assert coverage["comments"]["unavailable"] == [9]
    assert coverage["issue"]["complete"] is True
    assert coverage["sub_issues"]["complete"] is True
    assert coverage["dependencies"]["complete"] is True
    assert coverage["project_overlay"]["complete"] is True


def test_v2_repo_issue_graph_exposes_provider_read_stats(tmp_path: Path) -> None:
    service, _ = _service(tmp_path)
    gateway = service.application.context.ticket_graphs
    gateway.read_stats = TicketGraphReadStats(
        source="live_full",
        provider_processes=2,
        captured_stdout_bytes=4096,
        provider_process_duration_ms=18.5,
        per_capability=(CapabilityReadStat(GraphEvidenceCapability.ISSUE, 2, 4096, 18.5),),
    )

    result = service.repo_issue_v2("demo", mode="graph", fresh=True)

    assert result["read_stats"] == {
        "source": "live_full",
        "provider_processes": 2,
        "captured_stdout_bytes": 4096,
        "provider_process_duration_ms": 18.5,
        "per_capability": [
            {
                "capability": "issue",
                "provider_processes": 2,
                "captured_stdout_bytes": 4096,
                "provider_process_duration_ms": 18.5,
            }
        ],
        "cache_miss_reason": "fresh_requested",
    }
    V2_TOOL_SPECS["repo_issue"].validate_output(result)


@pytest.mark.anyio
async def test_repo_issue_graph_protocol_exposes_read_stats(tmp_path: Path) -> None:
    service, _ = _service(tmp_path)
    gateway = service.application.context.ticket_graphs
    gateway.read_stats = TicketGraphReadStats(
        source="live_full",
        provider_processes=3,
        captured_stdout_bytes=2048,
        provider_process_duration_ms=22.0,
    )
    server = create_server(service=service)

    async with create_connected_server_and_client_session(server) as session:
        result = await session.call_tool(
            "repo_issue", {"repo_id": "demo", "mode": "graph", "fresh": True}
        )

    assert result.isError is False
    payload = result.structuredContent
    assert payload is not None
    assert payload["read_stats"]["source"] == "live_full"
    assert payload["read_stats"]["provider_processes"] == 3
    assert payload["read_stats"]["captured_stdout_bytes"] == 2048
    assert payload["read_stats"]["cache_miss_reason"] == "fresh_requested"
    V2_TOOL_SPECS["repo_issue"].validate_output(payload)


@pytest.mark.anyio
async def test_repo_issue_graph_protocol_exposes_metadata_defaulted_in_drift(
    tmp_path: Path,
) -> None:
    service, environment = _service(tmp_path)
    environment.gh_state.write_text(
        json.dumps(
            {
                "issues": {},
                "diagnostics": [
                    {
                        "code": "METADATA_DEFAULTED",
                        "issue_number": 3,
                        "message": "status metadata is missing; defaulted to backlog for readiness",
                    },
                    {
                        "code": "PROJECT_OVERLAY_UNAVAILABLE",
                        "issue_number": 3,
                        "message": "project overlay could not be read",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    server = create_server(service=service)

    async with create_connected_server_and_client_session(server) as session:
        result = await session.call_tool(
            "repo_issue", {"repo_id": "demo", "mode": "graph", "fresh": True}
        )

    assert result.isError is False
    payload = result.structuredContent
    assert payload is not None
    codes = {item["code"] for item in payload["drift"]}
    assert "METADATA_DEFAULTED" in codes
    assert "PROJECT_OVERLAY_UNAVAILABLE" in codes
    assert all(item["issue_number"] == 3 for item in payload["drift"] if item["code"] in codes)
    V2_TOOL_SPECS["repo_issue"].validate_output(payload)


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
        "capabilities": [],
        "diagnostics_count": 1,
        "diagnostics_truncated": False,
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


def test_repo_issue_tools_render_evolution_and_exclude_superseded_work(tmp_path: Path) -> None:
    service, environment = _service(tmp_path)
    program = _node(
        3,
        ticket_type="program",
        status="In progress",
        parent=None,
        children=[40, 41, 50],
    )
    old = _node(40, status="Ready")
    replacement = _node(41, status="Ready")
    partial = _node(50, status="Ready")
    _write_manifest(environment.source, [program, old, replacement, partial])
    environment.gh_state.write_text(
        json.dumps(
            {
                "issues": {
                    "3": {"state": "OPEN"},
                    "40": {
                        "state": "OPEN",
                        "comments": [{"body": "Superseded by: #41"}],
                    },
                    "41": {
                        "state": "OPEN",
                        "comments": [{"body": "Supersedes: #40"}],
                    },
                    "50": {
                        "state": "CLOSED",
                        "comments": [
                            {
                                "body": (
                                    "Verified deliverables:\n- Parser verified\n"
                                    "Remaining scope:\n- Public adapter remains\n"
                                    "New child issues: #51"
                                )
                            }
                        ],
                    },
                }
            }
        ),
        encoding="utf-8",
    )

    graph = service.repo_issue_graph("demo")
    graph_nodes = {item["number"]: item for item in graph["nodes"]}
    assert graph_nodes[40]["evolution"]["relations"] == [
        {
            "type": "superseded_by",
            "target_issue": 41,
            "reason": "Declared superseded_by relation in live issue metadata.",
        }
    ]

    next_result = service.repo_issue_next("demo", limit=10)
    assert [item["number"] for item in next_result["tickets"]] == [41]
    assessments = {item["number"]: item for item in next_result["assessments"]}
    assert assessments[40]["derived_status"] == "Superseded"
    assert assessments[40]["evolution"]["relations"][0]["target_issue"] == 41
    assert assessments[50]["reason_codes"] == ["PARTIAL_COMPLETION_REMAINS"]
    assert assessments[50]["evolution"]["partial_completion"]["remaining_scope"] == [
        "Public adapter remains"
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
    assert result["evolution"] == {
        "relations": [],
        "partial_completion": None,
        "superseded_by": None,
    }


def test_repo_issue_spec_skips_metadata_drift_when_issue_capability_is_incomplete(
    tmp_path: Path,
) -> None:
    service, environment = _service(tmp_path)
    program = _node(3, ticket_type="program", status="In progress", parent=None, children=[9])
    ticket = _node(9, priority="P0", status="Done")
    _write_manifest(environment.source, [program, ticket])
    environment.gh_state.write_text(
        json.dumps(
            {
                "issues": {"9": {"state": "OPEN"}},
                "capability_coverage": [
                    {"capability": "issue", "complete": False, "unavailable": [9]},
                ],
            }
        ),
        encoding="utf-8",
    )

    result = service.repo_issue_spec("demo", 9, fresh=True)

    assert result["graph_member"] is True
    assert result["capability_coverage"] == [
        {"capability": "issue", "complete": False, "unavailable": [9], "truncated": False}
    ]
    # Manifest expects DONE/CLOSED but the live fixture reports OPEN -- without the fix this
    # would raise a LIVE_STATE_DRIFT diagnostic even though the graph's own evidence for this
    # issue is known-incomplete and should not be trusted for comparison.
    assert result["drift"] == [
        {
            "code": "GRAPH_EVIDENCE_INCOMPLETE_FOR_ISSUE",
            "message": (
                "graph metadata for this issue (status/priority/type) could not be fully "
                "resolved from GitHub; skipping metadata drift comparison to avoid comparing "
                "against a defaulted value"
            ),
        }
    ]


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


def test_repo_issue_spec_detects_stale_status_without_a_graph_node(tmp_path: Path) -> None:
    """#187 addendum 2: drift checks must run for any issue, graph member or
    not. An issue that is not enrolled in the ticket graph still declares its
    own Status metadata in its body; that self-declared status must still be
    checked against the issue's live open/closed state."""
    service, environment = _service(tmp_path)
    environment.gh_state.write_text(
        json.dumps(
            {
                "issues": {
                    "999": {
                        "body": (
                            "Objective: ship it.\n"
                            "Acceptance criteria: it works.\n"
                            "Tests: run the gate.\n"
                            "Status: Done."
                        ),
                        "state": "OPEN",
                        "comments": [],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    result = service.repo_issue_spec("demo", 999, fresh=True)
    assert result["graph_member"] is False
    assert result["node"] is None
    assert {
        "code": "LIVE_STATE_DRIFT",
        "message": "expected GitHub state CLOSED, got OPEN",
    } in result["drift"]


def test_repo_issue_spec_detects_a_closed_declared_blocker_without_a_graph_node(
    tmp_path: Path,
) -> None:
    """#187 addendum 2 / #195: closed-blocker drift must be detectable
    without ticket-graph membership. An issue that is not enrolled in the
    graph still declares its blockers in its own body; a declared blocker
    that is already closed on GitHub is a stale reference worth surfacing."""
    service, environment = _service(tmp_path)
    environment.gh_state.write_text(
        json.dumps(
            {
                "issues": {
                    "999": {
                        "body": (
                            "Objective: ship it.\n"
                            "Acceptance criteria: it works.\n"
                            "Tests: run the gate.\n"
                            "Blocked by: #106."
                        ),
                        "state": "OPEN",
                        "comments": [],
                    },
                    "106": {"state": "CLOSED"},
                }
            }
        ),
        encoding="utf-8",
    )
    result = service.repo_issue_spec("demo", 999, fresh=True)
    assert result["graph_member"] is False
    assert {
        "code": "STALE_BLOCKER_REFERENCE",
        "message": "declared blocker #106 is already closed on GitHub",
    } in result["drift"]


def test_repo_issue_spec_does_not_flag_a_blocker_that_is_still_open(tmp_path: Path) -> None:
    """A declared blocker that is still open is not a stale reference."""
    service, environment = _service(tmp_path)
    environment.gh_state.write_text(
        json.dumps(
            {
                "issues": {
                    "999": {
                        "body": (
                            "Objective: ship it.\n"
                            "Acceptance criteria: it works.\n"
                            "Tests: run the gate.\n"
                            "Blocked by: #106."
                        ),
                        "state": "OPEN",
                        "comments": [],
                    },
                    "106": {"state": "OPEN"},
                }
            }
        ),
        encoding="utf-8",
    )
    result = service.repo_issue_spec("demo", 999, fresh=True)
    assert not any(item["code"] == "STALE_BLOCKER_REFERENCE" for item in result["drift"])


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
        "correlation_hash",
        "origin",
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
        "correlation_hash",
        "origin",
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


# ---------------------------------------------------------------------------
# Round-2 review regression (#411): negative cache age must not be served
# ---------------------------------------------------------------------------


def _single_node_snapshot(*, repository_slug: str, observed_at: str) -> TicketGraphSnapshot:
    graph = TicketGraph(
        1,
        1,
        (
            TicketNode(
                1,
                "Program",
                TicketType.PROGRAM,
                TicketPriority.P0,
                TicketStatus.IN_PROGRESS,
                None,
                (),
                (),
                (),
                ("github",),
            ),
        ),
    )
    coverage = tuple(
        CapabilityCoverage(capability, True, (), False) for capability in GraphEvidenceCapability
    )
    return TicketGraphSnapshot(
        graph,
        observed_at,
        True,
        (),
        False,
        (TicketLiveMetadata(1, "Program", "OPEN", "body"),),
        coverage,
        repository_slug=repository_slug,
    )


class CountingTicketGraphGateway:
    def __init__(self, snapshot: TicketGraphSnapshot) -> None:
        self.snapshot = snapshot
        self.calls = 0

    def read(
        self,
        cwd: Path,
        source: GitHubTicketGraphConfig,
        *,
        max_items: int,
        remote: str = "origin",
    ) -> TicketGraphSnapshot:
        del cwd, source, max_items, remote
        self.calls += 1
        return self.snapshot


def _graph_context_with_cache(
    tmp_path: Path,
    *,
    clock: FixedClock,
    gateway: CountingTicketGraphGateway,
) -> tuple[ApplicationContext, RepositoryConfig, GitHubTicketGraphConfig]:
    repo_path = tmp_path / "repo"
    repo_path.mkdir(exist_ok=True)
    (repo_path / ".git").mkdir(exist_ok=True)
    state_root = tmp_path / "state"
    source = GitHubTicketGraphConfig(root_issue=1, repository="owner/demo")
    config = AppConfig(
        tmp_path / "config.toml",
        ServerConfig(
            tmp_path / "workspaces",
            state_root,
            github_read_cache_ttl_seconds=60,
            github_read_cache_authority_digest="a" * 64,
        ),
        {"demo": RepositoryConfig("demo", repo_path, ticket_graph=source)},
    )
    locks = InMemoryLockManager()
    audit = JsonlAuditSink(state_root, clock)
    cache = JsonGitHubReadCache(state_root, locks)
    ctx = ApplicationContext(
        config=config,
        commands=object(),
        git=object(),
        github=object(),
        filesystem=object(),
        store=InMemoryWorkspaceStore(),
        locks=locks,
        gate=InMemoryOperationGate(),
        audit=audit,
        clock=clock,
        ids=SequenceIdGenerator(),
        executables=object(),
        execution=execution_coordinator_for_tests(),
        github_read_cache=cache,
        ticket_graphs=gateway,
    )
    return ctx, config.repositories["demo"], source


def test_observed_age_ms_clamps_negative_and_rejects_skew() -> None:
    now_epoch = datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()
    past = datetime.fromtimestamp(now_epoch - 60, tz=timezone.utc)
    assert _observed_age_ms(now_epoch, past.isoformat()) == 60000.0
    future_within_skew = datetime.fromtimestamp(now_epoch + 2, tz=timezone.utc)
    assert _observed_age_ms(now_epoch, future_within_skew.isoformat()) == 0.0
    future_beyond_skew = datetime.fromtimestamp(now_epoch + 3600, tz=timezone.utc)
    assert _observed_age_ms(now_epoch, future_beyond_skew.isoformat()) is None
    assert _observed_age_ms(now_epoch, "not-a-timestamp") is None
    assert _observed_age_ms(now_epoch, "2026-01-01T00:00:00") is None


def test_negative_cache_age_ms_treated_as_miss(tmp_path: Path) -> None:
    clock = FixedClock("2026-01-01T00:00:00+00:00")
    fresh = _single_node_snapshot(
        repository_slug="owner/demo", observed_at="2026-01-02T00:00:00+00:00"
    )
    gateway = CountingTicketGraphGateway(fresh)
    ctx, repo, source = _graph_context_with_cache(tmp_path, clock=clock, gateway=gateway)

    cached_snapshot = _single_node_snapshot(
        repository_slug="owner/demo", observed_at="2026-07-16T00:00:00+00:00"
    )
    authority_digest = "a" * 64
    ctx.github_read_cache.put(
        "demo",
        repo.path,
        "graph",
        source.root_issue,
        _snapshot_payload(cached_snapshot, source, authority_digest),
        now_epoch=ctx.now_epoch(),
    )

    snapshot, cache_hit, cache_context = read_github_ticket_snapshot(
        ctx, repo, root_issue=1, fresh=False
    )

    assert cache_hit is False
    assert cache_context["miss_reason"] == "clock_skew"
    assert gateway.calls == 1, "a future-dated cache envelope must not be served"
    assert snapshot is not None
    assert snapshot.repository_slug == "owner/demo"


def test_graph_cache_fails_closed_without_pinned_authority(tmp_path: Path) -> None:
    """A graph cache must never be served or written when no authority digest is
    pinned: RepoForge cannot prove the ambient GitHub authority is the same one
    that wrote the entry, so serving it would leak evidence under a different
    credential context (round-2 #411)."""
    repo_path = tmp_path / "repo"
    repo_path.mkdir(exist_ok=True)
    (repo_path / ".git").mkdir(exist_ok=True)
    state_root = tmp_path / "state"
    source = GitHubTicketGraphConfig(root_issue=1, repository="owner/demo")
    config = AppConfig(
        tmp_path / "config.toml",
        ServerConfig(tmp_path / "workspaces", state_root, github_read_cache_ttl_seconds=60),
        {"demo": RepositoryConfig("demo", repo_path, ticket_graph=source)},
    )
    clock = FixedClock("2026-01-01T00:00:00+00:00")
    fresh = _single_node_snapshot(
        repository_slug="owner/demo", observed_at="2026-01-01T00:00:00+00:00"
    )
    gateway = CountingTicketGraphGateway(fresh)
    locks = InMemoryLockManager()
    audit = JsonlAuditSink(state_root, clock)
    cache = JsonGitHubReadCache(state_root, locks)
    ctx = ApplicationContext(
        config=config,
        commands=object(),
        git=object(),
        github=object(),
        filesystem=object(),
        store=InMemoryWorkspaceStore(),
        locks=locks,
        gate=InMemoryOperationGate(),
        audit=audit,
        clock=clock,
        ids=SequenceIdGenerator(),
        executables=object(),
        execution=execution_coordinator_for_tests(),
        github_read_cache=cache,
        ticket_graphs=gateway,
    )
    repo = config.repositories["demo"]

    snapshot, cache_hit, cache_context = read_github_ticket_snapshot(
        ctx, repo, root_issue=1, fresh=False
    )

    assert cache_hit is False
    assert cache_context["miss_reason"] == "authority_not_pinned"
    assert gateway.calls == 1, "the provider must be read when authority is not pinned"
    assert snapshot is not None
    assert (
        cache.get(
            "demo",
            repo.path,
            "graph",
            source.root_issue,
            ttl_seconds=60,
            now_epoch=ctx.now_epoch(),
        )
        is None
    ), "a cache entry must not be written under an unpinned authority"


def test_graph_cache_digest_mismatch_is_a_miss(tmp_path: Path) -> None:
    """Rotating the pinned authority digest invalidates every prior cache entry
    instead of serving private evidence under the wrong credential context."""
    clock = FixedClock("2026-01-01T00:00:00+00:00")
    fresh = _single_node_snapshot(
        repository_slug="owner/demo", observed_at="2026-01-01T00:00:00+00:00"
    )
    gateway = CountingTicketGraphGateway(fresh)
    ctx, repo, source = _graph_context_with_cache(tmp_path, clock=clock, gateway=gateway)
    cached_snapshot = _single_node_snapshot(
        repository_slug="owner/demo", observed_at="2026-01-01T00:00:00+00:00"
    )
    ctx.github_read_cache.put(
        "demo",
        repo.path,
        "graph",
        source.root_issue,
        _snapshot_payload(cached_snapshot, source, "b" * 64),
        now_epoch=ctx.now_epoch(),
    )

    _, cache_hit, cache_context = read_github_ticket_snapshot(ctx, repo, root_issue=1, fresh=False)

    assert cache_hit is False
    assert cache_context["miss_reason"] == "bindings_mismatch"
    assert gateway.calls == 1, "a cache entry pinned to a different authority must not be served"


def test_graph_cache_hit_requires_matching_pinned_authority(tmp_path: Path) -> None:
    """A cache entry pinned to the current authority digest and fresh under the
    TTL is served without provider traffic."""
    clock = FixedClock("2026-01-01T00:00:00+00:00")
    fresh = _single_node_snapshot(
        repository_slug="owner/demo", observed_at="2026-01-01T00:00:00+00:00"
    )
    gateway = CountingTicketGraphGateway(fresh)
    ctx, repo, source = _graph_context_with_cache(tmp_path, clock=clock, gateway=gateway)
    cached_snapshot = _single_node_snapshot(
        repository_slug="owner/demo", observed_at="2026-01-01T00:00:00+00:00"
    )
    ctx.github_read_cache.put(
        "demo",
        repo.path,
        "graph",
        source.root_issue,
        _snapshot_payload(cached_snapshot, source, "a" * 64),
        now_epoch=ctx.now_epoch(),
    )

    snapshot, cache_hit, cache_context = read_github_ticket_snapshot(
        ctx, repo, root_issue=1, fresh=False
    )

    assert cache_hit is True
    assert cache_context["hit_reason"] == "valid_bindings_ttl_fresh"
    assert gateway.calls == 0, "a valid pinned authority cache hit must not call the provider"
    assert snapshot is not None
    assert snapshot.repository_slug == "owner/demo"
