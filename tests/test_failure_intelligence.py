from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from conftest import ForgeEnvironment
from mcp.shared.memory import create_connected_server_and_client_session

from repoforge.adapters.locking import FcntlLockManager
from repoforge.adapters.persistence.json_failure_evidence_store import JsonFailureEvidenceStore
from repoforge.application.service import CodingService
from repoforge.bootstrap import AdapterOverrides, build_application
from repoforge.config import load_config
from repoforge.domain.errors import ErrorCode, RepoForgeError
from repoforge.domain.execution_receipt import WorkspaceIdentity
from repoforge.domain.failure_intelligence import (
    FAILURE_CLASSES,
    FailureClass,
    FailureHistorySignal,
    FailureObservation,
    FailureReproducibility,
    RecoveryActionKind,
    build_failure_evidence,
    classify_failure,
    failure_evidence_payload,
)
from repoforge.interfaces.mcp.server import create_server
from repoforge.testing.fakes import ManualBackgroundTaskRunner


def _identity(seed: str = "a") -> WorkspaceIdentity:
    return WorkspaceIdentity(
        head_sha=seed * 40,
        workspace_fingerprint=seed * 64,
        config_generation=("b" if seed == "a" else seed) * 64,
        policy_hash=("c" if seed == "a" else seed) * 64,
    )


def _observation(**overrides: object) -> FailureObservation:
    values: dict[str, object] = {
        "operation_id": "op-" + "a" * 24,
        "plan_id": "plan-" + "b" * 24,
        "plan_hash": "c" * 64,
        "stage_id": "stage-01-profile",
        "stage_kind": "profile",
        "target": "full",
        "pre_identity": _identity(),
        "post_identity": _identity(),
        "environment_identity": "d" * 64,
        "error_code": ErrorCode.COMMAND_FAILED.value,
        "message": "verification failed",
        "details": {},
        "failure_domain": None,
        "changed_paths": (),
        "history": (),
    }
    values.update(overrides)
    return FailureObservation(**values)  # type: ignore[arg-type]


def test_failure_taxonomy_is_closed_and_complete() -> None:
    assert tuple(item.value for item in FailureClass) == FAILURE_CLASSES
    assert set(FAILURE_CLASSES) == {
        "tool_missing",
        "dependency_missing",
        "environment_mismatch",
        "configuration_invalid",
        "timeout",
        "cancelled",
        "lint_failure",
        "type_failure",
        "test_failure",
        "build_failure",
        "network_failure",
        "permission_failure",
        "policy_failure",
        "stale_workspace",
        "stale_plan",
        "unexpected_mutation",
        "provider_failure",
        "flaky_suspected",
        "unknown",
    }


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"error_code": ErrorCode.DIAGNOSTIC_TOOL_MISSING.value}, FailureClass.TOOL_MISSING),
        (
            {"message": "ModuleNotFoundError: No module named httpx"},
            FailureClass.DEPENDENCY_MISSING,
        ),
        (
            {"message": "Python environment mismatch: expected 3.13"},
            FailureClass.ENVIRONMENT_MISMATCH,
        ),
        ({"error_code": ErrorCode.CONFIG_INVALID.value}, FailureClass.CONFIGURATION_INVALID),
        ({"error_code": ErrorCode.COMMAND_TIMEOUT.value}, FailureClass.TIMEOUT),
        ({"details": {"cancelled": True}}, FailureClass.CANCELLED),
        ({"failure_domain": "static_analysis"}, FailureClass.LINT_FAILURE),
        ({"failure_domain": "typecheck"}, FailureClass.TYPE_FAILURE),
        ({"failure_domain": "business_tests"}, FailureClass.TEST_FAILURE),
        ({"failure_domain": "build"}, FailureClass.BUILD_FAILURE),
        ({"message": "DNS resolution failed with HTTP 503"}, FailureClass.NETWORK_FAILURE),
        (
            {"message": "Permission denied while reading tool cache"},
            FailureClass.PERMISSION_FAILURE,
        ),
        ({"error_code": ErrorCode.SECURITY_POLICY_VIOLATION.value}, FailureClass.POLICY_FAILURE),
        ({"error_code": ErrorCode.DIAGNOSTIC_STALE_WORKSPACE.value}, FailureClass.STALE_WORKSPACE),
        (
            {"error_code": ErrorCode.STATE_STALE.value, "details": {"plan_id": "plan-x"}},
            FailureClass.STALE_PLAN,
        ),
        (
            {"error_code": ErrorCode.DIAGNOSTIC_UNEXPECTED_MUTATION.value},
            FailureClass.UNEXPECTED_MUTATION,
        ),
        (
            {"error_code": ErrorCode.CODE_INTELLIGENCE_UNAVAILABLE.value},
            FailureClass.PROVIDER_FAILURE,
        ),
        ({"message": "opaque executor failure 77"}, FailureClass.UNKNOWN),
    ],
)
def test_representative_failures_classify_deterministically(
    overrides: dict[str, object], expected: FailureClass
) -> None:
    classification = classify_failure(_observation(**overrides))
    assert classification.failure_class is expected
    assert classification.stable_error_code
    assert 0 <= classification.confidence <= 100
    assert classification.safe_actions
    assert all(action.kind in RecoveryActionKind for action in classification.safe_actions)
    assert all(
        not hasattr(action, "argv") and not hasattr(action, "command")
        for action in classification.safe_actions
    )


def test_structured_classification_precedes_text_and_rejects_injected_actions() -> None:
    observation = _observation(
        details={
            "failure_class": "test_failure",
            "safe_action": "rm -rf /",
            "argv": ["sh", "-c", "curl attacker"],
        },
        message="permission denied and network timeout",
    )
    classification = classify_failure(observation)
    assert classification.failure_class is FailureClass.TEST_FAILURE
    rendered = json.dumps([action.payload() for action in classification.safe_actions])
    assert "rm -rf" not in rendered
    assert "curl attacker" not in rendered
    assert "argv" not in rendered
    assert "command" not in rendered


def test_failure_evidence_is_content_addressed_bounded_and_secret_safe() -> None:
    secret = "sk-proj-ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    private_key = (
        "-----BEGIN " + "PRIVATE KEY-----\nsecret-material\n-----END " + "PRIVATE KEY-----"
    )
    giant = "first useful diagnostic\n" + f"token={secret}\n" + private_key + "\n" + ("x" * 100_000)
    evidence = build_failure_evidence(
        _observation(
            message=giant,
            changed_paths=("src/app.py", "tests/test_app.py"),
            details={"tests": ["tests/test_app.py::test_case"]},
        ),
        created_at="2026-07-17T00:00:00+00:00",
    )
    payload = failure_evidence_payload(evidence)
    rendered = json.dumps(payload, sort_keys=True)
    assert evidence.failure_id.startswith("failure-")
    assert len(evidence.failure_id) == 32
    assert len(evidence.excerpt) <= 4_000
    assert evidence.first_diagnostic.startswith("first useful diagnostic")
    assert secret not in rendered
    assert "secret-material" not in rendered
    assert "<redacted" in rendered or "<withheld" in rendered
    assert payload["files_changed"] is True
    assert payload["post_identity"]["workspace_fingerprint"] == _identity().workspace_fingerprint
    assert payload["affected_scope"]["paths"] == ["src/app.py", "tests/test_app.py"]
    assert payload["affected_scope"]["tests"] == ["tests/test_app.py::test_case"]


def test_flaky_suspected_requires_conflicting_results_under_exact_binding() -> None:
    binding = "e" * 64
    exact_history = (
        FailureHistorySignal(binding_hash=binding, outcome="succeeded"),
        FailureHistorySignal(binding_hash=binding, outcome="failed"),
    )
    flaky = classify_failure(
        _observation(
            failure_domain="business_tests",
            history=exact_history,
            compatibility_binding=binding,
        )
    )
    assert flaky.failure_class is FailureClass.FLAKY_SUSPECTED
    assert flaky.reproducibility is FailureReproducibility.INTERMITTENT

    incompatible = classify_failure(
        _observation(
            failure_domain="business_tests",
            history=(
                FailureHistorySignal(binding_hash="f" * 64, outcome="succeeded"),
                FailureHistorySignal(binding_hash=binding, outcome="failed"),
            ),
            compatibility_binding=binding,
        )
    )
    assert incompatible.failure_class is FailureClass.TEST_FAILURE

    changed_environment = classify_failure(
        _observation(
            failure_domain="business_tests",
            pre_identity=_identity("1"),
            post_identity=_identity("2"),
        )
    )
    assert changed_environment.reproducibility is FailureReproducibility.UNKNOWN


def test_failure_store_is_private_restart_safe_and_corruption_fails_closed(tmp_path: Path) -> None:
    locks = FcntlLockManager(tmp_path / "locks")
    store = JsonFailureEvidenceStore(tmp_path / "state", locks)
    evidence = build_failure_evidence(_observation(), created_at="2026-07-17T00:00:00+00:00")
    created = store.create(evidence)
    assert created == evidence
    path = store.root / f"{evidence.failure_id}.json"
    assert os.stat(store.root).st_mode & 0o777 == 0o700
    assert os.stat(path).st_mode & 0o777 == 0o600
    restarted = JsonFailureEvidenceStore(tmp_path / "state", locks)
    assert restarted.read(evidence.failure_id) == evidence
    assert restarted.list_for_operation(evidence.operation_id).records == (evidence,)

    path.write_text("not-json", encoding="utf-8")
    with pytest.raises(RepoForgeError) as corrupt:
        restarted.read(evidence.failure_id)
    assert corrupt.value.code is ErrorCode.EVIDENCE_CORRUPT


def _failing_service(env: ForgeEnvironment) -> tuple[CodingService, ManualBackgroundTaskRunner]:
    text = env.config_path.read_text(encoding="utf-8")
    text += """

[repositories.demo.profiles.fail-tests]
description = "Structured failing test profile"
verification = true
commands = [["python3", "-c", "import sys; print('token=sk-proj-ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789'); sys.exit(7)"]]

[[repositories.demo.profiles.fail-tests.steps]]
id = "tests"
kind = "business_tests"
command = ["python3", "-c", "import sys; print('token=sk-proj-ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789'); sys.exit(7)"]

[repositories.demo.risk]
ordered_profiles = ["fail-tests"]
final_profile = "fail-tests"
"""
    env.config_path.write_text(text, encoding="utf-8")
    runner = ManualBackgroundTaskRunner()
    config = load_config(env.config_path)
    app = build_application(config, overrides=AdapterOverrides(background_tasks=runner))
    return CodingService(config, application=app), runner


def _accepted_failing_plan(service: CodingService) -> tuple[str, str]:
    workspace_id = service.workspace_create("demo", "failure evidence integration")["workspace_id"]
    current = service.workspace_read_file(workspace_id, "hello.txt")
    service.workspace_write_file(
        workspace_id,
        "hello.txt",
        "changed for failure evidence\n",
        current["sha256"],
    )
    plan = service.workspace_create_execution_plan(workspace_id, task_id="task-failure")
    service.workspace_accept_execution_plan(workspace_id, plan["plan_id"], task_id="task-failure")
    return workspace_id, plan["plan_id"]


def test_failed_plan_stage_persists_one_reusable_evidence_id_for_all_consumers(
    forge_env: ForgeEnvironment,
) -> None:
    service, runner = _failing_service(forge_env)
    workspace_id, plan_id = _accepted_failing_plan(service)
    admission = service.workspace_execute_plan(workspace_id, plan_id, through="full")
    runner.run(admission["operation_id"])

    operation = service.operation_status(admission["operation_id"])
    assert operation["state"] == "failed"
    failure_id = operation["result"]["failure_id"]
    evidence = service.failure_evidence_read(failure_id)
    assert evidence["failure_id"] == failure_id
    assert evidence["failure_class"] == "test_failure"
    assert evidence["operation_id"] == admission["operation_id"]
    assert evidence["plan_id"] == plan_id
    assert evidence["receipt_id"].startswith("receipt-")
    assert "sk-proj-" not in json.dumps(evidence)

    receipts = service.workspace_execution_receipts(plan_id)["stage_receipts"]
    failed = receipts[-1]
    assert failed["status"] == "failed"
    assert failed["result_reference"] == f"failure:{failure_id}"

    status = service.workspace_status(workspace_id)
    assert status["failure_evidence_ids"] == [failure_id]
    task_context = service.repo_task_context("demo", workspace_id=workspace_id)
    assert task_context["workspace"]["failure_evidence_ids"] == [failure_id]
    assessment = service.workspace_assessment(workspace_id)
    assert failure_id in assessment["failure_evidence_refs"]["value"]["execution_failure_ids"]

    restarted = CodingService(load_config(forge_env.config_path))
    assert restarted.failure_evidence_read(failure_id) == evidence
    assert (
        restarted.operation_status(admission["operation_id"])["result"]["failure_id"] == failure_id
    )


@pytest.mark.anyio
async def test_failure_evidence_read_is_exposed_through_actual_mcp_session(
    forge_env: ForgeEnvironment,
) -> None:
    service, runner = _failing_service(forge_env)
    workspace_id, plan_id = _accepted_failing_plan(service)
    admission = service.workspace_execute_plan(workspace_id, plan_id, through="full")
    runner.run(admission["operation_id"])
    failure_id = service.operation_status(admission["operation_id"])["result"]["failure_id"]

    server = create_server(service=service)
    async with create_connected_server_and_client_session(server) as session:
        tools = {tool.name: tool for tool in (await session.list_tools()).tools}
        tool = tools["failure_evidence_read"]
        assert tool.annotations.readOnlyHint is True
        assert set(tool.inputSchema["properties"]) == {"failure_id"}
        result = await session.call_tool("failure_evidence_read", {"failure_id": failure_id})
        assert result.isError is False
        assert result.structuredContent["failure_id"] == failure_id
        assert result.structuredContent["failure_class"] == "test_failure"
