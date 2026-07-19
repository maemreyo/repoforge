from __future__ import annotations

import json
from pathlib import Path

from repoforge.adapters.github.gh_cli import GhCliGateway
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


def test_snapshot_flags_truncated_issue_identity_scan_and_item_fetch(tmp_path: Path) -> None:
    """A bounded issue-identity page scan or item-list fetch that never confirmed it saw
    every relevant record must flag `identities_truncated`/`items_truncated` rather than
    silently reporting a snapshot the planner would treat as ground truth."""
    gateway, executor = _gateway(tmp_path)

    def issue_page(argv: tuple[str, ...]) -> CommandResult:
        endpoint = next(item for item in argv if "page=" in item)
        page = int(endpoint.rsplit("page=", 1)[1])
        start = 5 + (page - 1) * 100
        records = [
            {"number": start + i, "id": 5000 + start + i, "node_id": f"ISSUE_{start + i}"}
            for i in range(100)
        ]
        return _result(json.dumps(records))

    item_limit = len(_graph().nodes) + 100  # matches gateway.snapshot's own computation
    items_page = [
        {
            "id": f"ITEM_{number}",
            "content": {"number": number, "repository": "maemreyo/repoforge"},
            "fieldValues": [],
        }
        for number in range(5, 5 + item_limit)
    ]

    executor.enqueue(
        _result("maemreyo/repoforge\n"),
        _result(json.dumps({"id": "PVT_1", "title": "Delivery"})),
        _result(json.dumps({"fields": []})),
        _result(json.dumps({"items": items_page})),
        _result(json.dumps({"data": {"node": {"views": {"nodes": []}}}})),
        *([issue_page] * 20),  # exhausts _MAX_ISSUE_PAGES without ever finding #1-#4
        _result(json.dumps([])),  # sub-issues for parent #1
        _result(json.dumps([])),  # sub-issues for parent #2
        _result(json.dumps([])),  # blocked-by for #3
    )

    snapshot = gateway.snapshot(tmp_path, _target(), _graph())

    assert snapshot.identities_truncated is True
    assert snapshot.items_truncated is True
    assert snapshot.issue_identities == {}


def test_snapshot_excludes_items_from_other_repositories_and_missing_repository_identity(
    tmp_path: Path,
) -> None:
    """A multi-repo Project can reuse the same issue number across repositories; an item
    must be excluded (not mapped by number alone) unless its repository identity is
    present and matches this repository, even when that identity is missing entirely."""
    gateway, executor = _gateway(tmp_path)
    executor.enqueue(
        _result("maemreyo/repoforge\n"),
        _result(json.dumps({"id": "PVT_1", "title": "Delivery"})),
        _result(json.dumps({"fields": []})),
        _result(
            json.dumps(
                {
                    "items": [
                        {
                            "id": "ITEM_own",
                            "content": {"number": 4, "repository": "maemreyo/repoforge"},
                            "fieldValues": [],
                        },
                        {
                            "id": "ITEM_other_repo",
                            "content": {"number": 3, "repository": "maemreyo/other-repo"},
                            "fieldValues": [],
                        },
                        {
                            "id": "ITEM_no_repo",
                            "content": {"number": 2},
                            "fieldValues": [],
                        },
                    ]
                }
            )
        ),
        _result(json.dumps({"data": {"node": {"views": {"nodes": []}}}})),
        _result(
            json.dumps(
                [
                    {"number": number, "id": 1000 + number, "node_id": f"ISSUE_{number}"}
                    for number in range(1, 5)
                ]
            )
        ),
        _result(json.dumps([])),
        _result(json.dumps([])),
        _result(json.dumps([])),
    )

    snapshot = gateway.snapshot(tmp_path, _target(), _graph())

    assert set(snapshot.items) == {4}
    assert snapshot.items[4].item_id == "ITEM_own"


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


def _issue_gateway(tmp_path: Path) -> tuple[GhCliGateway, ScriptedCommandExecutor]:
    executor = ScriptedCommandExecutor()
    server = ServerConfig(tmp_path / "workspaces", tmp_path / "state")
    return GhCliGateway(executor, server), executor


def test_issue_comment_posts_bounded_body_with_exact_rest_command(tmp_path: Path) -> None:
    gateway, executor = _issue_gateway(tmp_path)
    executor.enqueue(
        _result("acme/demo\n"),
        _result(
            json.dumps(
                {
                    "id": 91,
                    "body": "evidence\n<!-- marker -->",
                    "html_url": "https://github.com/acme/demo/issues/7#issuecomment-91",
                }
            )
        ),
    )

    comment = gateway.issue_comment(tmp_path, 7, "evidence\n<!-- marker -->")

    assert comment.comment_id == 91
    assert comment.body.endswith("<!-- marker -->")
    assert executor.calls[1] == (
        "gh",
        "api",
        "--method",
        "POST",
        "repos/acme/demo/issues/7/comments",
        "-f",
        "body=evidence\n<!-- marker -->",
    )


def test_issue_native_relationship_commands_use_database_ids(tmp_path: Path) -> None:
    gateway, executor = _issue_gateway(tmp_path)
    executor.enqueue(
        _result("acme/demo\n"),
        _result(json.dumps({"id": 501, "number": 9, "title": "Child", "state": "open"})),
        _result("acme/demo\n"),
        _result(json.dumps({"id": 501, "number": 9, "title": "Child", "state": "open"})),
    )

    sub_issue = gateway.add_sub_issue(tmp_path, 7, 501)
    blocker = gateway.add_blocked_by(tmp_path, 7, 501)

    assert sub_issue.issue_number == 9
    assert blocker.issue_number == 9
    assert executor.calls[1] == (
        "gh",
        "api",
        "--method",
        "POST",
        "repos/acme/demo/issues/7/sub_issues",
        "-F",
        "sub_issue_id=501",
    )
    assert executor.calls[3] == (
        "gh",
        "api",
        "--method",
        "POST",
        "repos/acme/demo/issues/7/dependencies/blocked_by",
        "-F",
        "issue_id=501",
    )


def test_issue_reconciliation_reads_are_capped_and_filter_pull_requests(tmp_path: Path) -> None:
    gateway, executor = _issue_gateway(tmp_path)
    executor.enqueue(
        _result("acme/demo\n"),
        _result(
            json.dumps(
                [
                    {"id": 1, "body": "one", "html_url": "https://example/1"},
                    {"id": 2, "body": "two", "html_url": "https://example/2"},
                ]
            )
        ),
        _result("acme/demo\n"),
        _result(
            json.dumps(
                [
                    {"id": 10, "number": 10, "title": "Issue", "state": "open", "body": "x"},
                    {
                        "id": 11,
                        "number": 11,
                        "title": "PR",
                        "state": "open",
                        "body": "y",
                        "pull_request": {},
                    },
                ]
            )
        ),
    )

    comments, comments_truncated = gateway.issue_comments(tmp_path, 7, max_comments=1)
    issues, issues_truncated = gateway.recent_issues(tmp_path, max_issues=1)

    assert [item.comment_id for item in comments] == [1]
    assert comments_truncated is True
    assert [item.issue_number for item in issues] == [10]
    assert issues_truncated is True
    assert "per_page=2" in executor.calls[1][4]
    assert "per_page=2" in executor.calls[3][4]
