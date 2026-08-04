from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest
from conftest import ForgeEnvironment
from mcp.shared.memory import create_connected_server_and_client_session

from repoforge.adapters.locking import FcntlLockManager
from repoforge.adapters.persistence.json_failure_evidence_store import JsonFailureEvidenceStore
from repoforge.application.service import CodingService
from repoforge.application.workspace.failure_intelligence import FailureIntelligenceService
from repoforge.bootstrap import AdapterOverrides, build_application
from repoforge.config import load_config
from repoforge.domain.errors import ErrorCode, RepoForgeError
from repoforge.domain.execution_receipt import WorkspaceIdentity
from repoforge.domain.failure_intelligence import (
    FAILURE_CLASSES,
    ChangedPathHash,
    FailureClass,
    FailureHistorySignal,
    FailureObservation,
    FailureReproducibility,
    RecoveryActionKind,
    build_failure_evidence,
    classify_failure,
    failure_evidence_from_payload,
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
        "workspace_id": "ws-demo-01",
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
    if "changed_path_hashes" not in overrides:
        # Most tests only care about `changed_paths`; derive a matching hash per path
        # so the FailureObservation subset invariant (changed_path_hashes <= changed_paths)
        # is satisfied without every existing override needing to supply hashes.
        changed_paths = values["changed_paths"]
        assert isinstance(changed_paths, tuple)
        values["changed_path_hashes"] = tuple(
            ChangedPathHash(path=path, sha256=hashlib.sha256(path.encode()).hexdigest())
            for path in changed_paths
        )
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


def test_a_failure_without_selectors_leads_with_the_persisted_output() -> None:
    """An empty selector list is not the absence of a failure.

    A real session hit a gate failure whose extraction produced no selectors, ran the
    same command standalone, saw it pass, and called the failure transient -- while the
    command's complete output sat persisted and addressable the whole time. The recovery
    evidence now names that read first, ahead of re-running anything.
    """
    reference = "failure-output:" + "f" * 64
    observation = _observation(
        failure_domain="business_tests",
        details={
            "selectors_unavailable_reason": "output_unrecognized",
            "output_artifact_reference": reference,
        },
    )

    classification = classify_failure(observation)

    first = classification.safe_actions[0]
    assert first.kind is RecoveryActionKind.RUNTIME_LOGS_READ
    assert first.payload()["arguments"] == {
        "source": "failure_artifact",
        "artifact_reference": reference,
    }
    assert "output_unrecognized" in first.precondition


def test_extracted_selectors_do_not_add_an_output_read_action() -> None:
    """When extraction worked, the selectors are the better evidence; adding a raw-output
    read would just be noise ahead of the profile re-run."""
    observation = _observation(
        failure_domain="business_tests",
        details={"output_artifact_reference": "failure-output:" + "a" * 64},
    )

    classification = classify_failure(observation)

    kinds = [action.kind for action in classification.safe_actions]
    assert RecoveryActionKind.RUNTIME_LOGS_READ not in kinds


def test_an_unretrievable_output_reference_yields_no_read_action() -> None:
    """An action pointing at output that cannot be fetched is worse than no action."""
    observation = _observation(
        failure_domain="business_tests",
        details={"selectors_unavailable_reason": "artifact_unavailable"},
    )

    classification = classify_failure(observation)

    kinds = [action.kind for action in classification.safe_actions]
    assert RecoveryActionKind.RUNTIME_LOGS_READ not in kinds


def test_recovery_actions_name_only_real_v2_tools_with_reconstructible_calls() -> None:
    """Every recovery action's kind must be one of the 28 currently-callable
    Forge v2 tools -- not a retired v1 tool name a client cannot execute.
    The public action must contain the target tool's exact `arguments`; this
    test deliberately has no per-kind translator that could hide a mismatch
    between recovery evidence and the real callable contract."""
    from repoforge.contracts.registry import V2_TOOL_NAMES, V2_TOOL_SPECS

    for overrides in (
        {"error_code": ErrorCode.DIAGNOSTIC_TOOL_MISSING.value},
        {"message": "ModuleNotFoundError: No module named httpx"},
        {"message": "Python environment mismatch: expected 3.13"},
        {"error_code": ErrorCode.CONFIG_INVALID.value},
        {"error_code": ErrorCode.COMMAND_TIMEOUT.value},
        {"details": {"cancelled": True}},
        {"failure_domain": "static_analysis"},
        {"failure_domain": "typecheck"},
        {"failure_domain": "business_tests"},
        {"failure_domain": "build"},
        {"message": "DNS resolution failed with HTTP 503"},
        {"message": "Permission denied while reading tool cache"},
        {"error_code": ErrorCode.SECURITY_POLICY_VIOLATION.value},
        {"error_code": ErrorCode.DIAGNOSTIC_STALE_WORKSPACE.value},
        {"error_code": ErrorCode.STATE_STALE.value, "details": {"plan_id": "plan-x"}},
        {"error_code": ErrorCode.DIAGNOSTIC_UNEXPECTED_MUTATION.value},
        {
            "error_code": ErrorCode.DIAGNOSTIC_UNEXPECTED_MUTATION.value,
            "changed_paths": ("src/a.py",),
        },
        {"error_code": ErrorCode.CODE_INTELLIGENCE_UNAVAILABLE.value},
        {"message": "opaque executor failure 77"},
        {
            "failure_domain": "business_tests",
            "details": {
                "selectors_unavailable_reason": "output_unrecognized",
                "output_artifact_reference": "failure-output:" + "e" * 64,
            },
        },
    ):
        observation = _observation(**overrides)
        classification = classify_failure(observation)
        for action in classification.safe_actions:
            assert action.kind.value in V2_TOOL_NAMES, (
                classification.failure_class,
                action.kind,
            )
            wire_action = action.payload()
            assert set(wire_action) == {"kind", "precondition", "arguments"}
            real_payload = wire_action["arguments"]
            assert isinstance(real_payload, dict)
            spec = V2_TOOL_SPECS[action.kind.value]
            validated = spec.validate_input(real_payload)
            assert validated is not None, (classification.failure_class, action.kind, real_payload)

            if action.kind is RecoveryActionKind.WORKSPACE_VERIFY and action.mode == "plan":
                if action.plan_action == "execute":
                    # execute operates on an existing accepted plan bound to
                    # this exact failed attempt's plan_id would just retry the
                    # binding that was already involved in the failure -- the
                    # only recovery actually offered is always a fresh plan.
                    raise AssertionError("execute must not be offered as a recovery action")
                if action.plan_action == "create":
                    assert action.plan_id is None
            if action.kind is RecoveryActionKind.OPERATION:
                assert action.operation_id == observation.operation_id
            if action.kind is RecoveryActionKind.WORKSPACE_MUTATE:
                assert action.relative_paths


def test_unexpected_mutation_recovery_action_uses_real_expected_sha256() -> None:
    """The restore recovery action's `expected_sha256` should reflect the real
    current-content hash computed by `FailureIntelligenceService.build()`, not the
    unconditional `None` this module previously had to emit for lack of filesystem
    access -- one path with a known hash, one confirmed absent (`None`)."""
    known_hash = "a" * 64
    observation = _observation(
        error_code=ErrorCode.DIAGNOSTIC_UNEXPECTED_MUTATION.value,
        changed_paths=("src/a.py", "src/b.py"),
        changed_path_hashes=(
            ChangedPathHash(path="src/a.py", sha256=known_hash),
            ChangedPathHash(path="src/b.py", sha256=None),
        ),
    )
    classification = classify_failure(observation)
    assert classification.failure_class is FailureClass.UNEXPECTED_MUTATION
    restore = next(
        action
        for action in classification.safe_actions
        if action.kind is RecoveryActionKind.WORKSPACE_MUTATE
    )
    entries = restore.payload()["arguments"]["operations"][0]["entries"]
    by_path = {entry["path"]: entry["expected_sha256"] for entry in entries}
    assert by_path == {"src/a.py": known_hash, "src/b.py": None}


def test_changed_path_hashes_must_be_subset_of_changed_paths() -> None:
    with pytest.raises(RepoForgeError):
        _observation(
            changed_paths=("src/a.py",),
            changed_path_hashes=(ChangedPathHash(path="src/other.py", sha256="a" * 64),),
        )


def test_recovery_action_path_hashes_only_valid_for_workspace_mutate() -> None:
    from repoforge.domain.failure_intelligence import RecoveryAction

    with pytest.raises(RepoForgeError):
        RecoveryAction(
            kind=RecoveryActionKind.WORKSPACE_STATUS,
            precondition="probe",
            workspace_id="ws-demo-01",
            relative_paths=("src/a.py",),
            path_hashes=(ChangedPathHash(path="src/a.py", sha256="a" * 64),),
        )


def test_workspace_mutate_recovery_action_round_trips_through_json_persistence(
    tmp_path: Path,
) -> None:
    """Persisted evidence is encoded with `RecoveryAction.payload()` (the public,
    reconstructable form), which nests restore paths under `operations[0].entries`
    rather than a top-level `relative_paths`/`paths` key. Decoding must extract paths
    (and now hashes) from that exact shape or a restored WORKSPACE_MUTATE action loses
    its paths entirely and fails `RecoveryAction.__post_init__`'s non-empty check."""
    known_hash = "b" * 64
    evidence = build_failure_evidence(
        _observation(
            error_code=ErrorCode.DIAGNOSTIC_UNEXPECTED_MUTATION.value,
            changed_paths=("src/a.py", "src/b.py"),
            changed_path_hashes=(
                ChangedPathHash(path="src/a.py", sha256=known_hash),
                ChangedPathHash(path="src/b.py", sha256=None),
            ),
        ),
        created_at="2026-07-30T00:00:00+00:00",
    )
    locks = FcntlLockManager(tmp_path / "locks")
    store = JsonFailureEvidenceStore(tmp_path / "state", locks)
    store.create(evidence)

    restarted = JsonFailureEvidenceStore(tmp_path / "state", locks)
    reloaded = restarted.read(evidence.failure_id)
    assert reloaded == evidence

    restore = next(
        action
        for action in reloaded.safe_actions
        if action.kind is RecoveryActionKind.WORKSPACE_MUTATE
    )
    assert set(restore.relative_paths) == {"src/a.py", "src/b.py"}
    by_path = {item.path: item.sha256 for item in restore.path_hashes}
    assert by_path == {"src/a.py": known_hash, "src/b.py": None}

    payload = failure_evidence_payload(reloaded)
    round_tripped = failure_evidence_from_payload(payload)
    assert round_tripped == reloaded


def test_failure_intelligence_service_computes_real_current_content_hashes(
    forge_env: ForgeEnvironment,
) -> None:
    """`FailureIntelligenceService` has the filesystem access the pure domain module
    lacks; it must fill in `changed_path_hashes` with the real current-content hash for
    an existing path and `None` for a path that does not exist."""
    config = load_config(forge_env.config_path)
    app = build_application(config)
    service = CodingService(config, application=app)
    workspace_id = service.workspace_create("demo", "hash probe")["workspace_id"]
    current = service.workspace_read_file(workspace_id, "hello.txt")
    service.workspace_write_file(
        workspace_id, "hello.txt", "changed for hashing\n", current["sha256"]
    )

    intelligence = FailureIntelligenceService(app.context)
    hashes = intelligence._changed_path_hashes(workspace_id, ("hello.txt", "does/not/exist.txt"))
    by_path = {item.path: item.sha256 for item in hashes}
    assert by_path["hello.txt"] == hashlib.sha256(b"changed for hashing\n").hexdigest()
    assert by_path["does/not/exist.txt"] is None


def test_stale_plan_recovery_never_recommends_reexecuting_the_known_stale_plan_id() -> None:
    """A stale-plan failure's own `observation.plan_id` is the plan that was
    just found stale; recommending `workspace_verify(plan_action="execute",
    plan_id=<that same id>)` would just reproduce the staleness. The only
    safe recovery is creating a fresh plan (#225 round-3 review: "a logical
    bug, not just a schema-shape bug")."""
    observation = _observation(
        error_code=ErrorCode.STATE_STALE.value, details={"plan_id": "plan-x"}
    )
    classification = classify_failure(observation)
    assert classification.failure_class is FailureClass.STALE_PLAN
    for action in classification.safe_actions:
        if action.kind is RecoveryActionKind.WORKSPACE_VERIFY:
            assert not (action.mode == "plan" and action.plan_action == "execute")


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
    """The static 28-tool Forge v2 surface has no standalone `failure_evidence_read`
    tool (#180); failure evidence is reachable only through `operation(action=
    "failure_evidence")` on the durable-operation composite."""
    service, runner = _failing_service(forge_env)
    workspace_id, plan_id = _accepted_failing_plan(service)
    admission = service.workspace_execute_plan(workspace_id, plan_id, through="full")
    runner.run(admission["operation_id"])
    failure_id = service.operation_status(admission["operation_id"])["result"]["failure_id"]

    server = create_server(service=service)
    async with create_connected_server_and_client_session(server) as session:
        tools = {tool.name: tool for tool in (await session.list_tools()).tools}
        assert "failure_evidence_read" not in tools
        tool = tools["operation"]
        assert "failure_id" in tool.inputSchema["properties"]
        result = await session.call_tool(
            "operation", {"action": "failure_evidence", "failure_id": failure_id}
        )
        assert result.isError is False
        assert result.structuredContent["action"] == "failure_evidence"
        assert result.structuredContent["failure_evidence"]["failure_id"] == failure_id
        assert result.structuredContent["failure_evidence"]["failure_class"] == "test_failure"
