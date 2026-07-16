from __future__ import annotations

import json
from pathlib import Path

from repoforge.adapters.github.ticket_project import GhTicketProjectGateway
from repoforge.config import ServerConfig
from repoforge.domain.ticket_sync import (
    TicketProjectOwnerType,
    TicketProjectTarget,
    TicketSyncChange,
    TicketSyncChangeKind,
)
from repoforge.domain.tickets import (
    TicketGraph,
    TicketNode,
    TicketPriority,
    TicketStatus,
    TicketType,
)
from repoforge.ports.command import CommandResult
from repoforge.testing.fakes import ScriptedCommandExecutor


def _graph() -> TicketGraph:
    return TicketGraph(
        schema_version=1,
        program_issue=1,
        nodes=(
            TicketNode(
                1,
                "Program",
                TicketType.PROGRAM,
                TicketPriority.P0,
                TicketStatus.IN_PROGRESS,
                None,
                (),
                (),
                (2,),
                ("master",),
            ),
            TicketNode(
                2,
                "Initiative",
                TicketType.INITIATIVE,
                TicketPriority.P1,
                TicketStatus.READY,
                1,
                (),
                (),
                (3, 4),
                ("master",),
            ),
            TicketNode(
                3,
                "Implementation",
                TicketType.IMPLEMENTATION_TICKET,
                TicketPriority.P1,
                TicketStatus.BLOCKED,
                2,
                (4,),
                (),
                (),
                ("master",),
            ),
            TicketNode(
                4,
                "Prerequisite",
                TicketType.IMPLEMENTATION_TICKET,
                TicketPriority.P0,
                TicketStatus.READY,
                2,
                (),
                (3,),
                (),
                ("master",),
            ),
        ),
    )


def _result(stdout: str = "", stderr: str = "", returncode: int = 0) -> CommandResult:
    return CommandResult(("gh",), "/repo", returncode, stdout, stderr)


def _gateway(tmp_path: Path) -> tuple[GhTicketProjectGateway, ScriptedCommandExecutor]:
    executor = ScriptedCommandExecutor()
    server = ServerConfig(tmp_path / "workspaces", tmp_path / "state")
    return GhTicketProjectGateway(executor, server), executor


def _target(
    owner_type: TicketProjectOwnerType = TicketProjectOwnerType.ORGANIZATION,
) -> TicketProjectTarget:
    return TicketProjectTarget("maemreyo", 7, owner_type)


def test_preflight_reports_auth_scopes_rate_limit_and_project_access(tmp_path: Path) -> None:
    gateway, executor = _gateway(tmp_path)
    executor.enqueue(
        _result(stderr="Token scopes: 'project', 'repo'"),
        _result(
            json.dumps(
                {
                    "resources": {
                        "core": {"remaining": 4999, "reset": 1784150400},
                        "graphql": {"remaining": 120, "reset": 1784154000},
                    }
                }
            )
        ),
        _result(json.dumps({"id": "PVT_1", "title": "Delivery"})),
    )

    result = gateway.preflight(tmp_path, _target(), apply=True)

    assert result.authenticated is True
    assert result.ready is True
    assert result.scopes == ("project", "repo")
    assert result.missing_scopes == ()
    assert result.rate_remaining == 120
    assert result.rate_reset is not None
    assert executor.calls == [
        ("gh", "auth", "status"),
        (
            "gh",
            "api",
            "--method",
            "GET",
            "rate_limit",
            "-H",
            "Accept: application/vnd.github+json",
            "-H",
            "X-GitHub-Api-Version: 2026-03-10",
        ),
        (
            "gh",
            "project",
            "view",
            "7",
            "--owner",
            "maemreyo",
            "--format",
            "json",
        ),
    ]


def test_preflight_fails_closed_when_classic_token_lacks_project_scope(tmp_path: Path) -> None:
    gateway, executor = _gateway(tmp_path)
    executor.enqueue(
        _result(stderr="Token scopes: 'repo'"),
        _result(json.dumps({"resources": {"core": {"remaining": 50, "reset": 1784150400}}})),
        _result(json.dumps({"id": "PVT_1", "title": "Delivery"})),
    )

    result = gateway.preflight(tmp_path, _target(), apply=True)

    assert result.ready is False
    assert result.missing_scopes == ("project",)


def test_snapshot_reads_project_fields_items_issue_ids_and_declared_edges(tmp_path: Path) -> None:
    gateway, executor = _gateway(tmp_path)
    executor.enqueue(
        _result("maemreyo/repoforge\n"),
        _result(json.dumps({"id": "PVT_1", "title": "Delivery"})),
        _result(
            json.dumps(
                {
                    "fields": [
                        {
                            "id": "FIELD_status",
                            "name": "Status",
                            "type": "ProjectV2SingleSelectField",
                            "options": [{"id": "OPT_ready", "name": "Ready"}],
                        }
                    ]
                }
            )
        ),
        _result(
            json.dumps(
                {
                    "items": [
                        {
                            "id": "ITEM_4",
                            "content": {"number": 4, "repository": "maemreyo/repoforge"},
                            "fieldValues": [{"field": {"name": "Status"}, "name": "Ready"}],
                        }
                    ]
                }
            )
        ),
        _result(
            json.dumps(
                {
                    "data": {
                        "node": {
                            "views": {
                                "nodes": [
                                    {
                                        "id": "VIEW_done",
                                        "name": "Done",
                                        "layout": "TABLE_LAYOUT",
                                        "filter": "Status:Done",
                                        "sortByFields": {"nodes": []},
                                    }
                                ]
                            }
                        }
                    }
                }
            )
        ),
        _result(
            json.dumps(
                [
                    {"number": number, "id": 1000 + number, "node_id": f"ISSUE_{number}"}
                    for number in range(1, 5)
                ]
            )
        ),
        _result(json.dumps([{"number": 2}])),
        _result(json.dumps([{"number": 3}, {"number": 4}])),
        _result(json.dumps([{"number": 4}])),
    )

    snapshot = gateway.snapshot(tmp_path, _target(), _graph())

    assert snapshot.project_id == "PVT_1"
    assert snapshot.fields["Status"].options == {"Ready": "OPT_ready"}
    assert snapshot.items[4].field_values == {"Status": "Ready"}
    assert snapshot.issue_identities[3].database_id == 1003
    assert snapshot.sub_issues == frozenset({(1, 2), (2, 3), (2, 4)})
    assert snapshot.blocked_by == frozenset({(3, 4)})
    assert snapshot.views["Done"].filter_query == "Status:Done"
    assert executor.calls[4][:4] == ("gh", "api", "graphql", "-f")
    assert "query RepoForgeTicketProjectViews" in executor.calls[4][4]
    assert executor.calls[5][4] == "repos/maemreyo/repoforge/issues?state=all&per_page=100&page=1"
    assert "--paginate" not in executor.calls[5]
    assert all(call[:2] == ("gh", "api") for call in executor.calls[4:])


def test_apply_change_uses_constrained_native_commands_and_view_fallback(tmp_path: Path) -> None:
    gateway, executor = _gateway(tmp_path)
    field = TicketSyncChange.create(
        TicketSyncChangeKind.CREATE_FIELD,
        {"name": "Priority", "data_type": "SINGLE_SELECT", "options": ["P0", "P1"]},
    )
    executor.enqueue(_result(json.dumps({"id": "FIELD_priority"})))
    assert gateway.apply_change(tmp_path, _target(), field)["status"] == "applied"
    assert executor.calls[-1] == (
        "gh",
        "project",
        "field-create",
        "7",
        "--owner",
        "maemreyo",
        "--name",
        "Priority",
        "--data-type",
        "SINGLE_SELECT",
        "--single-select-options",
        "P0,P1",
        "--format",
        "json",
    )

    sub_issue = TicketSyncChange.create(
        TicketSyncChangeKind.ADD_SUB_ISSUE,
        {"parent": 1, "child": 2, "parent_issue_id": 1001, "child_issue_id": 1002},
    )
    executor.enqueue(_result("maemreyo/repoforge\n"), _result(json.dumps({"number": 2})))
    gateway.apply_change(tmp_path, _target(), sub_issue)
    assert executor.calls[-1] == (
        "gh",
        "api",
        "--method",
        "POST",
        "repos/maemreyo/repoforge/issues/1/sub_issues",
        "-H",
        "Accept: application/vnd.github+json",
        "-H",
        "X-GitHub-Api-Version: 2026-03-10",
        "-F",
        "sub_issue_id=1002",
    )

    dependency = TicketSyncChange.create(
        TicketSyncChangeKind.ADD_BLOCKED_BY,
        {"issue": 3, "blocker": 4, "issue_id": 1003, "blocker_issue_id": 1004},
    )
    executor.enqueue(_result("maemreyo/repoforge\n"), _result(json.dumps({"number": 4})))
    gateway.apply_change(tmp_path, _target(), dependency)
    assert executor.calls[-1][-2:] == ("-F", "issue_id=1004")
    assert "dependencies/blocked_by" in executor.calls[-1][4]

    view = TicketSyncChange.create(
        TicketSyncChangeKind.CREATE_VIEW,
        {
            "name": "Ready Queue",
            "layout": "table",
            "filter_query": "Status:Ready",
            "sort_by": [["Priority", "asc"], ["Sequence", "asc"]],
        },
    )
    executor.enqueue(_result(json.dumps({"value": {"node_id": "VIEW_ready"}})))
    result = gateway.apply_change(tmp_path, _target(), view)
    assert executor.calls[-1][:5] == (
        "gh",
        "api",
        "--method",
        "POST",
        "orgs/maemreyo/projectsV2/7/views",
    )
    assert result["manual_actions"] == [
        "Configure Ready Queue sorting in GitHub: Priority asc, Sequence asc."
    ]
