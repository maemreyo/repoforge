from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from repoforge.adapters.github.graph_decode import failed_capabilities
from repoforge.adapters.github.ticket_graph import CommandGitHubTicketGraphGateway
from repoforge.config import GitHubTicketGraphConfig, ServerConfig
from repoforge.domain.errors import CommandError
from repoforge.domain.tickets import (
    GraphEvidenceCapability,
    TicketGraphError,
    TicketPriority,
    TicketStatus,
)
from repoforge.ports.cancellation import CancellationToken
from repoforge.ports.command import CommandResult

_QUERY_NUMBERS = re.compile(r"issue\(number: (\d+)\)")
_QUERY_SLUG = re.compile(r'repository\(owner: "([^"]+)", name: "([^"]+)"\)')


def _graphql_issue(
    number: int,
    title: str,
    *,
    state: str = "OPEN",
    body: str = "",
    labels: Sequence[str] = (),
    children: Sequence[int] = (),
    blocked_by: Sequence[int] = (),
    comments: Sequence[str] = (),
    external_children: Sequence[tuple[int, str]] = (),
    external_blocked_by: Sequence[tuple[int, str]] = (),
) -> dict[str, object]:
    return {
        "number": number,
        "title": title,
        "state": state,
        "body": body,
        "labels": {"totalCount": len(labels), "nodes": [{"name": label} for label in labels]},
        "subIssues": {
            "totalCount": len(children) + len(external_children),
            "nodes": [{"number": child} for child in children]
            + [
                {"number": child_number, "repository": {"nameWithOwner": child_repo}}
                for child_number, child_repo in external_children
            ],
        },
        "blockedBy": {
            "totalCount": len(blocked_by) + len(external_blocked_by),
            "nodes": [{"number": blocker} for blocker in blocked_by]
            + [
                {"number": blocker_number, "repository": {"nameWithOwner": blocker_repo}}
                for blocker_number, blocker_repo in external_blocked_by
            ],
        },
        "comments": {"totalCount": len(comments), "nodes": [{"body": c} for c in comments]},
    }


class GraphQLExecutor:
    """Fakes ``gh api graphql`` batches, honoring per-issue field failures."""

    def __init__(self, issues: Mapping[int, dict[str, object]]) -> None:
        self.issues = dict(issues)
        self.calls: list[tuple[str, ...]] = []
        self.failing: dict[int, set[str]] = {}
        self.truncate_after_first: bool = False
        self.partial_on_failure: bool = False
        self.project_failure: bool = False
        self.missing_without_error: set[int] = set()

    def environment(self, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        return dict(extra or {})

    @staticmethod
    def _field_requested(query: str, field: str) -> bool:
        return f"{field}(first" in query

    @staticmethod
    def _page_size(query: str, field: str) -> int | None:
        match = re.search(rf"{field}\(first: (\d+)\)", query)
        return int(match.group(1)) if match else None

    @staticmethod
    def _bounded_connection(field: str, value: object, query: str) -> object:
        page_size = GraphQLExecutor._page_size(query, field)
        if page_size is None or not isinstance(value, dict):
            return value
        nodes = value.get("nodes")
        if isinstance(nodes, list):
            value = {**value, "nodes": nodes[:page_size]}
        return value

    def _graphql_response(self, command: tuple[str, ...], query: str) -> CommandResult:
        numbers = [int(value) for value in _QUERY_NUMBERS.findall(query)]
        slug_match = _QUERY_SLUG.search(query)
        owner, name = slug_match.groups() if slug_match else ("acme", "widgets")
        slug = f"{owner}/{name}"
        data: dict[str, object] = {}
        errors: list[dict[str, object]] = []
        for index, number in enumerate(numbers):
            alias = f"r{index}"
            if number in self.missing_without_error:
                data[alias] = {"issue": None}
                continue
            if "issue" in self.failing.get(number, set()):
                data[alias] = {"issue": None}
                errors.append(
                    {
                        "type": "NOT_FOUND",
                        "path": [alias, "issue"],
                        "message": f"Could not resolve to an Issue with the number of {number}.",
                    }
                )
                continue
            issue = self.issues.get(number)
            if issue is None:
                data[alias] = {"issue": None}
                errors.append(
                    {
                        "type": "NOT_FOUND",
                        "path": [alias, "issue"],
                        "message": f"Could not resolve to an Issue with the number of {number}.",
                    }
                )
                continue
            stripped: dict[str, object] = {}
            failed_field = None
            for field, value in issue.items():
                if field in self.failing.get(number, set()):
                    if self._field_requested(query, field):
                        failed_field = field
                        break
                    continue
                bounded = self._bounded_connection(field, value, query)
                if field in {"subIssues", "blockedBy"} and isinstance(bounded, dict):
                    nodes = bounded.get("nodes")
                    if isinstance(nodes, list):
                        bounded = {
                            **bounded,
                            "nodes": [
                                node
                                if not isinstance(node, dict) or "repository" in node
                                else {**node, "repository": {"nameWithOwner": slug}}
                                for node in nodes
                            ],
                        }
                stripped[field] = bounded
            if failed_field is not None:
                if self.partial_on_failure:
                    partial = dict(issue)
                    partial[failed_field] = None
                    data[alias] = {"issue": partial}
                else:
                    data[alias] = {"issue": None}
                errors.append(
                    {
                        "type": "FORBIDDEN",
                        "path": [alias, "issue", failed_field],
                        "message": f"{failed_field} failed",
                    }
                )
            else:
                data[alias] = {"issue": stripped}
        payload: dict[str, object] = {"data": data}
        if errors:
            payload["errors"] = errors
        return CommandResult(
            command,
            str(Path(".").resolve()),
            1 if errors else 0,
            json.dumps(payload),
            "",
            stdout_truncated=self.truncate_after_first and len(self.calls) > 1,
        )

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
        cancel_token: CancellationToken | None = None,
    ) -> CommandResult:
        del input_text, timeout, check, extra_env, output_limit, cancel_token
        command = tuple(argv)
        self.calls.append(command)
        if command[:3] == ("gh", "repo", "view"):
            return CommandResult(command, str(cwd), 0, "acme/widgets\n", "")
        if command[:3] == ("gh", "api", "graphql"):
            query = next(item for item in command if item.startswith("query="))
            return self._graphql_response(command, query[len("query=") :])
        if command[:4] == ("gh", "project", "item-list"):
            if self.project_failure:
                raise CommandError("project item-list failed")
            return CommandResult(command, str(cwd), 0, json.dumps({"items": []}), "")
        raise CommandError(f"unhandled command: {command}")

    def run_bytes(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        timeout: int | None = None,
        max_bytes: int,
    ) -> bytes:
        del argv, cwd, timeout, max_bytes
        raise AssertionError("run_bytes is not used by graph reads")


def _fixture_issues() -> dict[int, dict[str, object]]:
    return {
        1: _graphql_issue(
            1,
            "Program",
            body="Status: in progress\nPriority: P0\nType: program",
            children=(2, 3),
        ),
        2: _graphql_issue(
            2,
            "First ticket",
            body="Status: ready\nPriority: P1\nType: implementation ticket",
            comments=("Superseded by: #3\nHandoff notes:\n- Continue in the canonical issue.",),
        ),
        3: _graphql_issue(
            3,
            "Second ticket",
            state="CLOSED",
            body="Priority: P2\nType: implementation ticket",
            blocked_by=(2,),
        ),
    }


def _gateway(executor: GraphQLExecutor, tmp_path: Path) -> CommandGitHubTicketGraphGateway:
    return CommandGitHubTicketGraphGateway(
        executor,
        ServerConfig(tmp_path / "workspaces", tmp_path / "state"),
    )


def test_reads_native_subissues_dependencies_and_metadata(tmp_path: Path) -> None:
    executor = GraphQLExecutor(_fixture_issues())
    gateway = _gateway(executor, tmp_path)

    snapshot = gateway.read(
        tmp_path,
        GitHubTicketGraphConfig(root_issue=1, repository="acme/widgets"),
        max_items=20,
    )

    nodes = {node.number: node for node in snapshot.graph.nodes}
    assert snapshot.graph.program_issue == 1
    assert snapshot.evidence_complete is True
    assert snapshot.unavailable == ()
    assert nodes[1].children == (2, 3)
    assert nodes[2].parent == 1
    assert nodes[2].status is TicketStatus.READY
    assert nodes[2].priority is TicketPriority.P1
    assert nodes[2].blocks == (3,)
    assert nodes[3].blockers == (2,)
    assert nodes[3].status is TicketStatus.DONE
    live = {item.number: item for item in snapshot.live_issues}
    assert set(live) == {1, 2, 3}
    assert live[2].comments == (
        "Superseded by: #3\nHandoff notes:\n- Continue in the canonical issue.",
    )
    assert snapshot.read_stats is not None
    assert snapshot.read_stats.provider_processes == 2
    assert all(call[0] == "gh" and call[:3] == ("gh", "api", "graphql") for call in executor.calls)
    coverage = {item.capability: item for item in snapshot.capability_coverage}
    assert {item.complete for item in coverage.values()} == {True}
    assert {item.unavailable for item in coverage.values()} == {()}


def test_marks_partial_evidence_when_one_dependency_read_fails(tmp_path: Path) -> None:
    executor = GraphQLExecutor(_fixture_issues())
    executor.failing[2] = {"blockedBy"}
    gateway = _gateway(executor, tmp_path)

    snapshot = gateway.read(tmp_path, GitHubTicketGraphConfig(root_issue=1), max_items=20)

    assert snapshot.evidence_complete is False
    assert snapshot.unavailable == (2,)
    assert {node.number for node in snapshot.graph.nodes} == {1, 2, 3}
    coverage = {item.capability: item for item in snapshot.capability_coverage}
    assert coverage[GraphEvidenceCapability.DEPENDENCIES].complete is False
    assert coverage[GraphEvidenceCapability.DEPENDENCIES].unavailable == (2,)
    assert coverage[GraphEvidenceCapability.ISSUE].complete is True
    assert coverage[GraphEvidenceCapability.SUB_ISSUES].complete is True
    assert coverage[GraphEvidenceCapability.COMMENTS].complete is True
    nodes = {node.number: node for node in snapshot.graph.nodes}
    assert nodes[2].status is TicketStatus.READY


def test_marks_partial_evidence_when_one_comments_read_fails(tmp_path: Path) -> None:
    executor = GraphQLExecutor(_fixture_issues())
    executor.failing[3] = {"comments"}
    gateway = _gateway(executor, tmp_path)

    snapshot = gateway.read(tmp_path, GitHubTicketGraphConfig(root_issue=1), max_items=20)

    coverage = {item.capability: item for item in snapshot.capability_coverage}
    assert coverage[GraphEvidenceCapability.COMMENTS].complete is False
    assert coverage[GraphEvidenceCapability.COMMENTS].unavailable == (3,)
    assert coverage[GraphEvidenceCapability.ISSUE].complete is True
    assert coverage[GraphEvidenceCapability.SUB_ISSUES].complete is True
    assert coverage[GraphEvidenceCapability.DEPENDENCIES].complete is True
    # A comments-only gap must not taint the issue metadata that title/state/type/priority
    # drift checks depend on.
    assert 3 not in coverage[GraphEvidenceCapability.ISSUE].unavailable


@pytest.mark.parametrize("max_items", [0, 201])
def test_rejects_unbounded_graph_reads(tmp_path: Path, max_items: int) -> None:
    gateway = _gateway(GraphQLExecutor({}), tmp_path)

    with pytest.raises(TicketGraphError, match="between 1 and 200"):
        gateway.read(tmp_path, GitHubTicketGraphConfig(root_issue=1), max_items=max_items)


def test_root_unavailable_raises(tmp_path: Path) -> None:
    gateway = _gateway(GraphQLExecutor({}), tmp_path)

    with pytest.raises(TicketGraphError, match="root #1 is unavailable"):
        gateway.read(tmp_path, GitHubTicketGraphConfig(root_issue=1), max_items=20)


def test_missing_child_issue_is_marked_unavailable(tmp_path: Path) -> None:
    issues = _fixture_issues()
    issues[1] = _graphql_issue(1, "Program", children=(2, 9))
    executor = GraphQLExecutor(issues)
    gateway = _gateway(executor, tmp_path)

    snapshot = gateway.read(tmp_path, GitHubTicketGraphConfig(root_issue=1), max_items=20)

    assert snapshot.evidence_complete is False
    assert snapshot.unavailable == (9,)
    issue_coverage = next(
        item
        for item in snapshot.capability_coverage
        if item.capability is GraphEvidenceCapability.ISSUE
    )
    assert 9 in issue_coverage.unavailable


def test_missing_metadata_defaults_with_repair_diagnostic(tmp_path: Path) -> None:
    issues = _fixture_issues()
    issues[2] = _graphql_issue(2, "First ticket", body="no status or priority markers")
    executor = GraphQLExecutor(issues)
    gateway = _gateway(executor, tmp_path)

    snapshot = gateway.read(tmp_path, GitHubTicketGraphConfig(root_issue=1), max_items=20)

    nodes = {node.number: node for node in snapshot.graph.nodes}
    assert nodes[2].status is TicketStatus.BACKLOG
    assert nodes[2].priority is TicketPriority.P3
    assert snapshot.unavailable == ()
    assert snapshot.evidence_complete is True
    codes = {diagnostic.code for diagnostic in snapshot.diagnostics}
    assert "METADATA_DEFAULTED" in codes
    coverage = {item.capability: item for item in snapshot.capability_coverage}
    assert coverage[GraphEvidenceCapability.ISSUE].complete is True
    assert 2 not in coverage[GraphEvidenceCapability.ISSUE].unavailable


def test_batched_40_node_graph_uses_bounded_requests(tmp_path: Path) -> None:
    issues: dict[int, dict[str, object]] = {
        1: _graphql_issue(1, "Program", children=tuple(range(2, 41)))
    }
    for number in range(2, 41):
        issues[number] = _graphql_issue(number, f"Ticket {number}")
    executor = GraphQLExecutor(issues)
    gateway = _gateway(executor, tmp_path)

    snapshot = gateway.read(
        tmp_path,
        GitHubTicketGraphConfig(root_issue=1, repository="acme/widgets"),
        max_items=200,
    )

    assert snapshot.graph.program_issue == 1
    assert len(snapshot.graph.nodes) == 40
    assert snapshot.read_stats is not None
    assert snapshot.read_stats.provider_processes <= 5
    assert snapshot.read_stats.provider_processes < 40
    assert all(call[:3] == ("gh", "api", "graphql") for call in executor.calls)


def test_batched_200_node_graph_counts_requests_and_processes(tmp_path: Path) -> None:
    issues: dict[int, dict[str, object]] = {
        1: _graphql_issue(1, "Program", children=tuple(range(2, 101)))
    }
    for number in range(2, 52):
        issues[number] = _graphql_issue(
            number, f"Ticket {number}", children=(number + 99, number + 149)
        )
    for number in range(52, 101):
        issues[number] = _graphql_issue(number, f"Ticket {number}")
    for number in range(101, 201):
        issues[number] = _graphql_issue(number, f"Ticket {number}")
    executor = GraphQLExecutor(issues)
    gateway = _gateway(executor, tmp_path)

    snapshot = gateway.read(
        tmp_path,
        GitHubTicketGraphConfig(root_issue=1, repository="acme/widgets"),
        max_items=200,
    )

    assert len(snapshot.graph.nodes) == 200
    assert snapshot.truncated is False
    assert snapshot.read_stats is not None
    assert snapshot.read_stats.provider_processes < 200
    assert all(call[:3] == ("gh", "api", "graphql") for call in executor.calls)


def test_truncated_graphql_output_marks_batch_unavailable(tmp_path: Path) -> None:
    executor = GraphQLExecutor(_fixture_issues())
    executor.truncate_after_first = True
    gateway = _gateway(executor, tmp_path)

    snapshot = gateway.read(
        tmp_path,
        GitHubTicketGraphConfig(root_issue=1, repository="acme/widgets"),
        max_items=20,
    )

    assert snapshot.evidence_complete is False
    assert set(snapshot.unavailable) == {2, 3}


def test_partial_graphql_object_retries_stripped_selection(tmp_path: Path) -> None:
    executor = GraphQLExecutor(_fixture_issues())
    executor.partial_on_failure = True
    executor.failing[2] = {"comments"}
    gateway = _gateway(executor, tmp_path)

    snapshot = gateway.read(tmp_path, GitHubTicketGraphConfig(root_issue=1), max_items=20)

    nodes = {node.number: node for node in snapshot.graph.nodes}
    assert 2 in nodes
    assert nodes[2].status is TicketStatus.READY
    coverage = {item.capability: item for item in snapshot.capability_coverage}
    assert coverage[GraphEvidenceCapability.COMMENTS].complete is False
    assert coverage[GraphEvidenceCapability.COMMENTS].unavailable == (2,)
    assert coverage[GraphEvidenceCapability.ISSUE].complete is True
    assert snapshot.evidence_complete is False
    graphql_calls = [call for call in executor.calls if call[:3] == ("gh", "api", "graphql")]
    assert len(graphql_calls) == 3
    retry_query = next(item for item in graphql_calls[-1] if item.startswith("query="))[
        len("query=") :
    ]
    assert "comments(first" not in retry_query


def test_project_overlay_failure_degrades_only_overlay_capability(tmp_path: Path) -> None:
    executor = GraphQLExecutor(_fixture_issues())
    executor.project_failure = True
    gateway = _gateway(executor, tmp_path)
    source = GitHubTicketGraphConfig(
        root_issue=1,
        repository="acme/widgets",
        project_owner="acme",
        project_number=7,
    )

    snapshot = gateway.read(tmp_path, source, max_items=20)

    nodes = {node.number: node for node in snapshot.graph.nodes}
    assert set(nodes) == {1, 2, 3}
    assert nodes[2].status is TicketStatus.READY
    assert nodes[3].status is TicketStatus.DONE
    coverage = {item.capability: item for item in snapshot.capability_coverage}
    assert coverage[GraphEvidenceCapability.PROJECT_OVERLAY].complete is False
    assert coverage[GraphEvidenceCapability.ISSUE].complete is True
    assert coverage[GraphEvidenceCapability.SUB_ISSUES].complete is True
    assert coverage[GraphEvidenceCapability.DEPENDENCIES].complete is True
    codes = {diagnostic.code for diagnostic in snapshot.diagnostics}
    assert "PROJECT_OVERLAY_UNAVAILABLE" in codes
    assert snapshot.evidence_complete is False


@pytest.mark.parametrize(
    ("child_count", "expect_truncated"),
    [(100, False), (101, True)],
)
def test_edge_page_truncation_uses_total_count_not_page_size(
    tmp_path: Path, child_count: int, expect_truncated: bool
) -> None:
    issues = {
        1: _graphql_issue(1, "Program", children=tuple(range(2, 2 + child_count))),
        **{
            number: _graphql_issue(number, f"Ticket {number}")
            for number in range(2, 2 + child_count)
        },
    }
    executor = GraphQLExecutor(issues)
    gateway = _gateway(executor, tmp_path)

    snapshot = gateway.read(tmp_path, GitHubTicketGraphConfig(root_issue=1), max_items=200)

    assert snapshot.truncated is expect_truncated
    coverage = {item.capability: item for item in snapshot.capability_coverage}
    assert coverage[GraphEvidenceCapability.SUB_ISSUES].truncated is expect_truncated


def test_more_than_fifty_labels_fail_closed_issue_metadata(tmp_path: Path) -> None:
    issues = _fixture_issues()
    many_labels = [f"label-{index:02d}" for index in range(51)]
    issues[2] = _graphql_issue(
        2, "First ticket", body="Status: ready\nPriority: P1", labels=many_labels
    )
    executor = GraphQLExecutor(issues)
    gateway = _gateway(executor, tmp_path)

    snapshot = gateway.read(tmp_path, GitHubTicketGraphConfig(root_issue=1), max_items=20)

    assert snapshot.evidence_complete is False
    coverage = {item.capability: item for item in snapshot.capability_coverage}
    assert coverage[GraphEvidenceCapability.ISSUE].complete is False
    assert 2 in coverage[GraphEvidenceCapability.ISSUE].unavailable


def test_successful_alias_has_no_failed_capabilities() -> None:
    assert failed_capabilities([], "r0") == set()
    assert (
        failed_capabilities([{"path": ["r1", "issue", "comments"], "message": "x"}], "r0") == set()
    )
    assert failed_capabilities([{"path": ["r0", "issue", "comments"], "message": "x"}], "r0") == {
        GraphEvidenceCapability.COMMENTS
    }


def test_partial_labels_failure_fails_issue_capability_closed(tmp_path: Path) -> None:
    executor = GraphQLExecutor(_fixture_issues())
    executor.partial_on_failure = True
    executor.failing[2] = {"labels"}
    gateway = _gateway(executor, tmp_path)

    snapshot = gateway.read(tmp_path, GitHubTicketGraphConfig(root_issue=1), max_items=20)

    assert 2 not in {node.number for node in snapshot.graph.nodes}
    assert snapshot.evidence_complete is False
    assert 2 in snapshot.unavailable
    coverage = {item.capability: item for item in snapshot.capability_coverage}
    assert coverage[GraphEvidenceCapability.ISSUE].complete is False
    assert 2 in coverage[GraphEvidenceCapability.ISSUE].unavailable


def test_partial_number_failure_is_rejected(tmp_path: Path) -> None:
    executor = GraphQLExecutor(_fixture_issues())
    executor.partial_on_failure = True
    executor.failing[2] = {"number"}
    gateway = _gateway(executor, tmp_path)

    snapshot = gateway.read(tmp_path, GitHubTicketGraphConfig(root_issue=1), max_items=20)

    assert 2 not in {node.number for node in snapshot.graph.nodes}
    assert 2 in snapshot.unavailable
    coverage = {item.capability: item for item in snapshot.capability_coverage}
    assert coverage[GraphEvidenceCapability.ISSUE].complete is False


def test_partial_body_failure_is_rejected(tmp_path: Path) -> None:
    executor = GraphQLExecutor(_fixture_issues())
    executor.partial_on_failure = True
    executor.failing[2] = {"body"}
    gateway = _gateway(executor, tmp_path)

    snapshot = gateway.read(tmp_path, GitHubTicketGraphConfig(root_issue=1), max_items=20)

    assert 2 not in {node.number for node in snapshot.graph.nodes}
    assert 2 in snapshot.unavailable
    coverage = {item.capability: item for item in snapshot.capability_coverage}
    assert coverage[GraphEvidenceCapability.ISSUE].complete is False


def test_missing_issue_without_error_fails_closed(tmp_path: Path) -> None:
    executor = GraphQLExecutor(_fixture_issues())
    executor.missing_without_error = {2}
    gateway = _gateway(executor, tmp_path)

    snapshot = gateway.read(tmp_path, GitHubTicketGraphConfig(root_issue=1), max_items=20)

    assert 2 not in {node.number for node in snapshot.graph.nodes}
    assert 2 in snapshot.unavailable
    coverage = {item.capability: item for item in snapshot.capability_coverage}
    assert coverage[GraphEvidenceCapability.ISSUE].complete is False


def test_cross_repository_subissue_is_not_hydrated(tmp_path: Path) -> None:
    issues = {
        1: _graphql_issue(1, "Program", external_children=[(42, "other/org")]),
        42: _graphql_issue(42, "Local ticket forty-two"),
    }
    executor = GraphQLExecutor(issues)
    gateway = _gateway(executor, tmp_path)

    snapshot = gateway.read(
        tmp_path,
        GitHubTicketGraphConfig(root_issue=1, repository="acme/widgets"),
        max_items=20,
    )

    nodes = {node.number: node for node in snapshot.graph.nodes}
    assert set(nodes) == {1}
    assert nodes[1].children == ()
    assert snapshot.evidence_complete is False
    assert 1 in snapshot.unavailable
    coverage = {item.capability: item for item in snapshot.capability_coverage}
    assert coverage[GraphEvidenceCapability.SUB_ISSUES].complete is False
    assert 1 in coverage[GraphEvidenceCapability.SUB_ISSUES].unavailable
    assert any(
        diagnostic.code == "CROSS_REPOSITORY_RELATION_UNSUPPORTED"
        and "other/org#42" in diagnostic.message
        for diagnostic in snapshot.diagnostics
    )


def test_cross_repository_blocker_fails_closed(tmp_path: Path) -> None:
    issues = {
        1: _graphql_issue(1, "Program", external_blocked_by=[(7, "other/org")]),
        7: _graphql_issue(7, "Local ticket seven"),
    }
    executor = GraphQLExecutor(issues)
    gateway = _gateway(executor, tmp_path)

    snapshot = gateway.read(
        tmp_path,
        GitHubTicketGraphConfig(root_issue=1, repository="acme/widgets"),
        max_items=20,
    )

    nodes = {node.number: node for node in snapshot.graph.nodes}
    assert nodes[1].blockers == ()
    assert snapshot.evidence_complete is False
    coverage = {item.capability: item for item in snapshot.capability_coverage}
    assert coverage[GraphEvidenceCapability.DEPENDENCIES].complete is False
    assert any(
        diagnostic.code == "CROSS_REPOSITORY_RELATION_UNSUPPORTED"
        and "other/org#7" in diagnostic.message
        for diagnostic in snapshot.diagnostics
    )


def test_malformed_labels_connection_fails_closed(tmp_path: Path) -> None:
    issues = _fixture_issues()
    issues[2] = {**issues[2], "labels": {"nodes": [{"name": "x"}]}}
    executor = GraphQLExecutor(issues)
    gateway = _gateway(executor, tmp_path)

    snapshot = gateway.read(tmp_path, GitHubTicketGraphConfig(root_issue=1), max_items=20)

    assert 2 in snapshot.unavailable
    coverage = {item.capability: item for item in snapshot.capability_coverage}
    assert coverage[GraphEvidenceCapability.ISSUE].complete is False
    assert 2 in coverage[GraphEvidenceCapability.ISSUE].unavailable


def test_malformed_edge_connection_fails_closed(tmp_path: Path) -> None:
    issues = _fixture_issues()
    issues[1] = {
        **issues[1],
        "subIssues": {"totalCount": 1, "nodes": ["bogus"]},
    }
    executor = GraphQLExecutor(issues)
    gateway = _gateway(executor, tmp_path)

    snapshot = gateway.read(tmp_path, GitHubTicketGraphConfig(root_issue=1), max_items=20)

    assert snapshot.evidence_complete is False
    assert 1 in snapshot.unavailable
    coverage = {item.capability: item for item in snapshot.capability_coverage}
    assert coverage[GraphEvidenceCapability.SUB_ISSUES].complete is False
    assert 1 in coverage[GraphEvidenceCapability.SUB_ISSUES].unavailable


def test_issue_number_mismatch_fails_closed(tmp_path: Path) -> None:
    issues = _fixture_issues()
    issues[2] = {**issues[2], "number": 999}
    executor = GraphQLExecutor(issues)
    gateway = _gateway(executor, tmp_path)

    snapshot = gateway.read(tmp_path, GitHubTicketGraphConfig(root_issue=1), max_items=20)

    assert 2 not in {node.number for node in snapshot.graph.nodes}
    assert 2 in snapshot.unavailable
    coverage = {item.capability: item for item in snapshot.capability_coverage}
    assert coverage[GraphEvidenceCapability.ISSUE].complete is False


def test_project_failure_is_not_traversal_truncation(tmp_path: Path) -> None:
    executor = GraphQLExecutor(_fixture_issues())
    executor.project_failure = True
    gateway = _gateway(executor, tmp_path)
    source = GitHubTicketGraphConfig(
        root_issue=1,
        repository="acme/widgets",
        project_owner="acme",
        project_number=7,
    )

    snapshot = gateway.read(tmp_path, source, max_items=20)

    assert {node.number for node in snapshot.graph.nodes} == {1, 2, 3}
    assert snapshot.truncated is False
    assert snapshot.evidence_complete is False
    coverage = {item.capability: item for item in snapshot.capability_coverage}
    assert coverage[GraphEvidenceCapability.PROJECT_OVERLAY].complete is False
    assert coverage[GraphEvidenceCapability.PROJECT_OVERLAY].truncated is False
    assert snapshot.read_stats is not None
    project_stat = next(
        item
        for item in snapshot.read_stats.per_capability
        if item.capability is GraphEvidenceCapability.PROJECT_OVERLAY
    )
    assert project_stat.provider_processes == 1
    assert snapshot.repository_slug == "acme/widgets"
