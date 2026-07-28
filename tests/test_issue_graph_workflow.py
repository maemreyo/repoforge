from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from conftest import create_forge_environment
from mcp import types as mcp_types
from mcp.shared.memory import create_connected_server_and_client_session

from repoforge.application.service import CodingService
from repoforge.bootstrap import AdapterOverrides, build_application
from repoforge.contracts.registry import V2_TOOL_SPECS
from repoforge.domain.approval import ApprovalStatus, decide_approval
from repoforge.domain.errors import ConfigError
from repoforge.domain.issue_writes import IssueWritePolicy
from repoforge.domain.runtime_contract import RuntimeContractIdentity
from repoforge.interfaces.mcp.server import create_server
from repoforge.ports.issue_mutation import RemoteComment, RemoteIssue


class MutableClock:
    def __init__(self, value: str) -> None:
        self.value = value

    def now_iso(self) -> str:
        return self.value


class ManualBackgroundRunner:
    def __init__(self) -> None:
        self.tasks: dict[str, Callable[[], None]] = {}

    def submit(self, key: str, task: Callable[[], None]) -> bool:
        if key in self.tasks:
            return False
        self.tasks[key] = task
        return True


class MemoryIssueGateway:
    def __init__(self) -> None:
        self.issues: dict[int, RemoteIssue] = {}
        self.sub_issue_numbers: dict[int, set[int]] = {}
        self.blocker_numbers: dict[int, set[int]] = {}
        self.external_writes = 0
        self._next = 300

    @staticmethod
    def _database_id(number: int) -> int:
        return 100_000 + number

    def issue_details(self, cwd: Path, issue_number: int) -> RemoteIssue:
        del cwd
        return self.issues[issue_number]

    def issue_comments(
        self, cwd: Path, issue_number: int, *, max_comments: int
    ) -> tuple[tuple[RemoteComment, ...], bool]:
        del cwd, issue_number, max_comments
        return (), False

    def recent_issues(self, cwd: Path, *, max_issues: int) -> tuple[tuple[RemoteIssue, ...], bool]:
        del cwd
        values = tuple(sorted(self.issues.values(), key=lambda item: item.issue_number))
        return values[:max_issues], len(values) > max_issues

    def issue_comment(self, cwd: Path, issue_number: int, body: str) -> RemoteComment:
        del cwd, issue_number, body
        raise AssertionError("issue graph workflow does not use comments")

    def set_issue_state(self, cwd: Path, issue_number: int, state: str) -> RemoteIssue:
        del cwd, issue_number, state
        raise AssertionError("issue graph workflow does not change issue state")

    def create_issue(self, cwd: Path, title: str, body: str) -> RemoteIssue:
        del cwd
        number = self._next
        self._next += 1
        issue = RemoteIssue(
            number,
            self._database_id(number),
            title,
            "open",
            body,
            f"https://github.test/issues/{number}",
        )
        self.issues[number] = issue
        self.external_writes += 1
        return issue

    def update_issue(self, cwd: Path, issue_number: int, *, title: str, body: str) -> RemoteIssue:
        del cwd
        current = self.issues[issue_number]
        updated = replace(current, title=title, body=body)
        self.issues[issue_number] = updated
        self.external_writes += 1
        return updated

    def sub_issues(
        self, cwd: Path, issue_number: int, *, max_issues: int
    ) -> tuple[tuple[RemoteIssue, ...], bool]:
        del cwd
        numbers = sorted(self.sub_issue_numbers.get(issue_number, set()))
        return tuple(self.issues[number] for number in numbers[:max_issues]), len(
            numbers
        ) > max_issues

    def blocked_by(
        self, cwd: Path, issue_number: int, *, max_issues: int
    ) -> tuple[tuple[RemoteIssue, ...], bool]:
        del cwd
        numbers = sorted(self.blocker_numbers.get(issue_number, set()))
        return tuple(self.issues[number] for number in numbers[:max_issues]), len(
            numbers
        ) > max_issues

    def add_sub_issue(self, cwd: Path, issue_number: int, sub_issue_id: int) -> RemoteIssue:
        del cwd
        child = next(item for item in self.issues.values() if item.database_id == sub_issue_id)
        self.sub_issue_numbers.setdefault(issue_number, set()).add(child.issue_number)
        self.external_writes += 1
        return self.issues[issue_number]

    def add_blocked_by(self, cwd: Path, issue_number: int, blocker_issue_id: int) -> RemoteIssue:
        del cwd
        blocker = next(
            item for item in self.issues.values() if item.database_id == blocker_issue_id
        )
        self.blocker_numbers.setdefault(issue_number, set()).add(blocker.issue_number)
        self.external_writes += 1
        return self.issues[issue_number]

    def remove_sub_issue(self, cwd: Path, issue_number: int, sub_issue_id: int) -> RemoteIssue:
        del cwd
        child = next(item for item in self.issues.values() if item.database_id == sub_issue_id)
        self.sub_issue_numbers.setdefault(issue_number, set()).discard(child.issue_number)
        self.external_writes += 1
        return self.issues[issue_number]

    def remove_blocked_by(self, cwd: Path, issue_number: int, blocker_issue_id: int) -> RemoteIssue:
        del cwd
        blocker = next(
            item for item in self.issues.values() if item.database_id == blocker_issue_id
        )
        self.blocker_numbers.setdefault(issue_number, set()).discard(blocker.issue_number)
        self.external_writes += 1
        return self.issues[issue_number]


def _runtime_identity() -> dict[str, object]:
    return {
        "server_build_sha": "1" * 64,
        "server_version": "2.2.0",
        "active_generation": 12,
        "tool_surface_hash": "2" * 64,
        "input_contract_digest": "3" * 64,
        "output_contract_digest": "4" * 64,
        "runtime_protocol_version": 1,
        "process_start_identity": "5" * 64,
    }


def _manage_plan() -> dict[str, object]:
    return {
        "action": "plan",
        "root_ref": "epic-232",
        "nodes": [
            {
                "client_ref": "epic-232",
                "title": "Control-plane truth hardening",
                "ticket_type": "epic",
                "priority": "p0",
                "status": "in_progress",
                "parent_ref": None,
                "body": "## Objective\n\nShip it.\n\n## Acceptance criteria\n\n- [ ] Done.\n",
            }
        ],
        "edges": [],
        "adopt_refs": [],
        "expires_in_seconds": 3600,
    }


def _service(
    tmp_path: Path,
    *,
    clock: MutableClock | None = None,
    operation_semantics_version: int = 2,
    enabled_ops: tuple[str, ...] = ("link", "create", "update"),
) -> tuple[CodingService, ManualBackgroundRunner, MemoryIssueGateway]:
    environment = create_forge_environment(tmp_path, clock=clock)
    repo = replace(
        environment.service.config.repositories["demo"],
        issue_writes=IssueWritePolicy(
            enabled_ops=enabled_ops,
            operation_semantics_version=operation_semantics_version,
            max_writes_per_call=20,
            max_writes_per_window=100,
            window_seconds=60,
        ),
    )
    config = replace(
        environment.service.config,
        repositories={**environment.service.config.repositories, "demo": repo},
    )
    runner = ManualBackgroundRunner()
    gateway = MemoryIssueGateway()
    application = build_application(
        config,
        overrides=AdapterOverrides(
            clock=clock,
            issue_mutations=gateway,
            background_tasks=runner,
        ),
    )
    return CodingService(config, application=application), runner, gateway


def _approve_planned_workflow(service: CodingService, planned: dict[str, Any]) -> None:
    approvals = service.application.context.approvals
    assert approvals is not None
    envelope = approvals.read(planned["approval_request_id"])
    assert envelope is not None
    approvals.save(
        decide_approval(
            envelope.value,
            ApprovalStatus.ACCEPTED,
            actor="operator",
            decided_at="2026-07-23T01:00:00+00:00",
            reason="Approved exact issue graph publication.",
        ),
        expected_revision=envelope.revision,
    )


@pytest.mark.parametrize(
    ("operation_semantics_version", "expected_state"),
    ((1, "succeeded"), (2, "publishing")),
)
def test_publication_migrates_legacy_create_authority_but_current_update_is_independent(
    tmp_path: Path,
    operation_semantics_version: int,
    expected_state: str,
) -> None:
    service, _, gateway = _service(
        tmp_path,
        operation_semantics_version=operation_semantics_version,
        enabled_ops=("link", "create"),
    )
    planned = service.repo_issue_v2(
        "demo",
        "manage",
        manage=_manage_plan(),
        runtime_identity=_runtime_identity(),
    )["workflow"]
    _approve_planned_workflow(service, planned)
    apply_request = {
        "action": "apply",
        "proposal_id": planned["proposal_id"],
        "proposal_hash": planned["proposal_hash"],
        "plan_id": planned["plan_id"],
        "effect_plan_hash": planned["effect_plan_hash"],
        "approval_request_id": planned["approval_request_id"],
    }
    if operation_semantics_version == 2:
        with pytest.raises(ConfigError, match="repo_issue update capability"):
            service.repo_issue_v2(
                "demo",
                "manage",
                manage=apply_request,
                runtime_identity=_runtime_identity(),
            )
        assert gateway.external_writes == 0
        assert not service._issue_graph_workflow.publications.list_publications().records
        operation_store = service.application.context.operation_store
        assert operation_store is not None
        assert not operation_store.list_records(max_records=2_000).records
        receipt_store = service.application.context.effect_receipts
        assert receipt_store is not None
        assert not receipt_store.list_all().records
        return

    applied = service.repo_issue_v2(
        "demo",
        "manage",
        manage=apply_request,
        runtime_identity=_runtime_identity(),
    )["workflow"]

    result = service.repo_issue_v2(
        "demo",
        "manage",
        manage={"action": "reconcile", "publication_id": applied["publication_id"]},
        runtime_identity=_runtime_identity(),
    )["workflow"]

    assert result["state"] == expected_state
    publication = service._issue_graph_workflow.publications.read_publication(
        applied["publication_id"]
    )
    assert publication is not None
    assert result["complete"] is True


def test_repo_issue_manage_plan_requires_exact_approval_and_writes_nothing(tmp_path: Path) -> None:
    service, _, gateway = _service(tmp_path)

    planned = service.repo_issue_v2(
        "demo",
        "manage",
        manage=_manage_plan(),
        runtime_identity=_runtime_identity(),
    )

    workflow = planned["workflow"]
    assert workflow["state"] == "pending_approval"
    assert workflow["proposal_id"].startswith("igp-")
    assert workflow["plan_id"].startswith("igplan-")
    assert workflow["approval_request_id"].startswith("apr-")
    assert workflow["approval_status"] == "pending"
    assert workflow["external_writes"] == 0
    assert gateway.external_writes == 0

    task_context = service.repo_task_context_v2(
        "demo",
        sections=["ticket_workflow"],
    )
    section = task_context["sections"][0]
    facts = {item["key"]: item["value"] for item in section["facts"]}
    assert section["name"] == "ticket_workflow"
    assert section["complete"] is True
    assert section["truncated"] is False
    assert facts["complete"] == "false"
    assert facts["plan_id"] == workflow["plan_id"]
    assert facts["approval_status"] == "pending"
    assert facts["recovery_action"] == (
        f"Run `rf approval approve {workflow['approval_request_id']}` after review, then "
        "retry the exact apply request."
    )


def test_repo_issue_manage_apply_reconnect_reconcile_and_complete(tmp_path: Path) -> None:
    service, runner, gateway = _service(tmp_path)
    planned = service.repo_issue_v2(
        "demo",
        "manage",
        manage=_manage_plan(),
        runtime_identity=_runtime_identity(),
    )["workflow"]
    approval_id = planned["approval_request_id"]
    approvals = service.application.context.approvals
    assert approvals is not None
    envelope = approvals.read(approval_id)
    assert envelope is not None
    approvals.save(
        decide_approval(
            envelope.value,
            ApprovalStatus.ACCEPTED,
            actor="operator",
            decided_at="2026-07-23T01:00:00+00:00",
            reason="Approved exact issue graph publication.",
        ),
        expected_revision=envelope.revision,
    )

    applied = service.repo_issue_v2(
        "demo",
        "manage",
        manage={
            "action": "apply",
            "proposal_id": planned["proposal_id"],
            "proposal_hash": planned["proposal_hash"],
            "plan_id": planned["plan_id"],
            "effect_plan_hash": planned["effect_plan_hash"],
            "approval_request_id": approval_id,
        },
        runtime_identity=_runtime_identity(),
    )["workflow"]

    assert applied["state"] == "publishing"
    assert applied["operation_id"].startswith("op-")
    assert applied["receipt_id"].startswith("receipt-")
    assert applied["complete"] is False
    assert gateway.external_writes == 0
    assert len(runner.tasks) == 1

    reconnected = CodingService(service.config, application=service.application)
    status = reconnected.repo_issue_v2(
        "demo",
        "manage",
        manage={"action": "status", "publication_id": applied["publication_id"]},
        runtime_identity=_runtime_identity(),
    )["workflow"]
    assert status["state"] == "publishing"
    assert status["operation_id"] == applied["operation_id"]
    assert status["receipt_id"] == applied["receipt_id"]
    audit_events = [
        json.loads(line)
        for line in (service.config.server.state_root / "audit.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line
    ]
    status_event = next(
        event
        for event in reversed(audit_events)
        if event["action"] == "repo_issue_manage" and event["details"]["action"] == "status"
    )
    assert status_event["details"]["is_mutating"] is False
    with pytest.raises(ConfigError, match="belongs to repository"):
        reconnected.repo_issue_v2(
            "other",
            "manage",
            manage={"action": "status", "publication_id": applied["publication_id"]},
            runtime_identity=_runtime_identity(),
        )

    completed = reconnected.repo_issue_v2(
        "demo",
        "manage",
        manage={"action": "reconcile", "publication_id": applied["publication_id"]},
        runtime_identity=_runtime_identity(),
    )["workflow"]
    assert completed["state"] == "succeeded"
    assert completed["complete"] is True
    assert completed["result_reference"].startswith("issue-graph-publication:")
    assert gateway.external_writes > 0


def test_repo_issue_manage_survives_process_restart(tmp_path: Path) -> None:
    clock = MutableClock("2026-07-23T00:00:00+00:00")
    service, _, gateway = _service(tmp_path, clock=clock)
    planned = service.repo_issue_v2(
        "demo",
        "manage",
        manage=_manage_plan(),
        runtime_identity=_runtime_identity(),
    )["workflow"]
    approval_id = planned["approval_request_id"]
    approvals = service.application.context.approvals
    assert approvals is not None
    envelope = approvals.read(approval_id)
    assert envelope is not None
    approvals.save(
        decide_approval(
            envelope.value,
            ApprovalStatus.ACCEPTED,
            actor="operator",
            decided_at=clock.now_iso(),
            reason="Approved exact issue graph publication.",
        ),
        expected_revision=envelope.revision,
    )
    applied = service.repo_issue_v2(
        "demo",
        "manage",
        manage={
            "action": "apply",
            "proposal_id": planned["proposal_id"],
            "proposal_hash": planned["proposal_hash"],
            "plan_id": planned["plan_id"],
            "effect_plan_hash": planned["effect_plan_hash"],
            "approval_request_id": approval_id,
        },
        runtime_identity=_runtime_identity(),
    )["workflow"]

    clock.value = "2026-07-23T00:20:00+00:00"
    restarted_application = build_application(
        service.config,
        overrides=AdapterOverrides(
            clock=clock,
            issue_mutations=gateway,
            background_tasks=ManualBackgroundRunner(),
        ),
    )
    restarted = CodingService(service.config, application=restarted_application)
    status = restarted.repo_issue_v2(
        "demo",
        "manage",
        manage={"action": "status", "publication_id": applied["publication_id"]},
        runtime_identity=_runtime_identity(),
    )["workflow"]
    assert status["state"] == "publishing"
    assert status["operation_id"] == applied["operation_id"]

    completed = restarted.repo_issue_v2(
        "demo",
        "manage",
        manage={"action": "reconcile", "publication_id": applied["publication_id"]},
        runtime_identity=_runtime_identity(),
    )["workflow"]
    assert completed["state"] == "succeeded"
    assert completed["complete"] is True


@pytest.mark.anyio
async def test_mcp_elicitation_accepts_exact_issue_graph_approval(tmp_path: Path) -> None:
    service, _, gateway = _service(tmp_path)
    identity = RuntimeContractIdentity(**_runtime_identity())
    messages: list[str] = []

    async def approve(_context: Any, params: Any) -> mcp_types.ElicitResult:
        messages.append(str(params.message))
        return mcp_types.ElicitResult(action="accept", content={"approve": True})

    server = create_server(
        service=service,
        contract_identity_provider=lambda: identity,
    )
    async with create_connected_server_and_client_session(
        server,
        elicitation_callback=approve,
    ) as session:
        result = await session.call_tool(
            "repo_issue",
            {"repo_id": "demo", "mode": "manage", "manage": _manage_plan()},
        )

    assert result.isError is False
    assert result.structuredContent is not None
    workflow = result.structuredContent["workflow"]
    assert workflow["approval_status"] == "accepted"
    assert workflow["state"] == "planned"
    assert messages and workflow["approval_request_id"] in messages[0]
    assert gateway.external_writes == 0
    approvals = service.application.context.approvals
    assert approvals is not None
    stored = approvals.read(workflow["approval_request_id"])
    assert stored is not None
    assert stored.value.status is ApprovalStatus.ACCEPTED


@pytest.mark.anyio
async def test_mcp_repo_issue_manage_injects_server_runtime_identity() -> None:
    identity = RuntimeContractIdentity(**_runtime_identity())
    captured: dict[str, Any] = {}

    class CaptureService:
        config: Any = None
        metrics: Any = None

        def repo_issue_v2(self, **kwargs: Any) -> dict[str, object]:
            captured.update(kwargs)
            return {
                "summary": "Planned governed issue graph",
                "repo_id": "demo",
                "mode": "manage",
                "graph_status": "not_requested",
                "workflow": {
                    "action": "plan",
                    "state": "pending_approval",
                    "proposal_id": "igp-" + "a" * 24,
                    "proposal_hash": "b" * 64,
                    "plan_id": "igplan-" + "c" * 24,
                    "effect_plan_hash": "d" * 64,
                    "approval_request_id": "apr-" + "e" * 24,
                    "approval_status": "pending",
                    "complete": False,
                    "external_writes": 0,
                    "recovery_action": "Approve the exact request, then retry apply.",
                },
            }

    server = create_server(
        service=CaptureService(),  # type: ignore[arg-type]
        contract_identity_provider=lambda: identity,
    )
    arguments = {
        "repo_id": "demo",
        "mode": "manage",
        "manage": {
            "action": "plan",
            "root_ref": "epic-232",
            "nodes": [
                {
                    "client_ref": "epic-232",
                    "title": "Control-plane truth hardening",
                    "ticket_type": "epic",
                    "priority": "p0",
                    "status": "in_progress",
                    "parent_ref": None,
                    "body": "Govern issue graph publication.",
                }
            ],
            "edges": [],
            "adopt_refs": [],
            "expires_in_seconds": 3600,
        },
    }

    async with create_connected_server_and_client_session(server) as session:
        result = await session.call_tool("repo_issue", arguments)

    assert result.isError is False
    assert result.structuredContent is not None
    assert result.structuredContent["workflow"]["approval_status"] == "pending"
    assert captured["runtime_identity"] == identity.as_dict()
    public_properties = V2_TOOL_SPECS["repo_issue"].input_model.model_json_schema()["properties"]
    assert "runtime_identity" not in public_properties
