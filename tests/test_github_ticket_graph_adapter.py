from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

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


class GraphExecutor:
    def __init__(self, responses: Mapping[str, object]) -> None:
        self.responses = dict(responses)
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
        cancel_token: CancellationToken | None = None,
    ) -> CommandResult:
        del input_text, timeout, check, extra_env, output_limit, cancel_token
        command = tuple(argv)
        self.calls.append(command)
        if command[:3] == ("gh", "repo", "view"):
            return CommandResult(command, str(cwd), 0, "acme/widgets\n", "")
        endpoint = next((item for item in command if item.startswith("repos/")), None)
        if endpoint is None or endpoint not in self.responses:
            raise CommandError(f"unhandled command: {command}")
        value = self.responses[endpoint]
        if isinstance(value, Exception):
            raise value
        return CommandResult(command, str(cwd), 0, json.dumps(value), "")

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


def _issue(number: int, title: str, body: str, *, state: str = "open") -> dict[str, object]:
    return {
        "number": number,
        "title": title,
        "state": state,
        "body": body,
        "labels": [],
    }


def _responses() -> dict[str, object]:
    prefix = "repos/acme/widgets/issues"
    return {
        f"{prefix}/1": _issue(
            1,
            "Program",
            "Status: in progress\nPriority: P0\nType: program",
        ),
        f"{prefix}/1/sub_issues?per_page=100": [{"number": 2}, {"number": 3}],
        f"{prefix}/1/comments?per_page=20": [],
        f"{prefix}/2": _issue(
            2,
            "First ticket",
            "Status: ready\nPriority: P1\nType: implementation ticket",
        ),
        f"{prefix}/2/sub_issues?per_page=100": [],
        f"{prefix}/2/comments?per_page=20": [
            {"body": "Superseded by: #3\nHandoff notes:\n- Continue in the canonical issue."}
        ],
        f"{prefix}/3": _issue(
            3,
            "Second ticket",
            "Priority: P2\nType: implementation ticket",
            state="closed",
        ),
        f"{prefix}/3/sub_issues?per_page=100": [],
        f"{prefix}/3/comments?per_page=20": [],
        f"{prefix}/1/dependencies/blocked_by?per_page=100": [],
        f"{prefix}/2/dependencies/blocked_by?per_page=100": [],
        f"{prefix}/3/dependencies/blocked_by?per_page=100": [{"number": 2}],
    }


def test_reads_native_subissues_dependencies_and_metadata(tmp_path: Path) -> None:
    executor = GraphExecutor(_responses())
    gateway = CommandGitHubTicketGraphGateway(
        executor,
        ServerConfig(tmp_path / "workspaces", tmp_path / "state"),
    )

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
    assert all(call[0] == "gh" for call in executor.calls)
    coverage = {item.capability: item for item in snapshot.capability_coverage}
    assert {item.complete for item in coverage.values()} == {True}
    assert {item.unavailable for item in coverage.values()} == {()}


def test_marks_partial_evidence_when_one_dependency_read_fails(tmp_path: Path) -> None:
    responses = _responses()
    responses["repos/acme/widgets/issues/2/dependencies/blocked_by?per_page=100"] = CommandError(
        "temporary GitHub failure"
    )
    gateway = CommandGitHubTicketGraphGateway(
        GraphExecutor(responses),
        ServerConfig(tmp_path / "workspaces", tmp_path / "state"),
    )

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


def test_marks_partial_evidence_when_one_comments_read_fails(tmp_path: Path) -> None:
    responses = _responses()
    responses["repos/acme/widgets/issues/3/comments?per_page=20"] = CommandError(
        "temporary GitHub failure"
    )
    gateway = CommandGitHubTicketGraphGateway(
        GraphExecutor(responses),
        ServerConfig(tmp_path / "workspaces", tmp_path / "state"),
    )

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
    gateway = CommandGitHubTicketGraphGateway(
        GraphExecutor({}),
        ServerConfig(tmp_path / "workspaces", tmp_path / "state"),
    )

    with pytest.raises(TicketGraphError, match="between 1 and 200"):
        gateway.read(tmp_path, GitHubTicketGraphConfig(root_issue=1), max_items=max_items)
