from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path

import pytest
from conftest import ForgeEnvironment

from repoforge.adapters.locking import FcntlLockManager
from repoforge.adapters.persistence.json_execution_plan_store import (
    JsonExecutionPlanAcceptanceStore,
    JsonExecutionPlanStore,
)
from repoforge.domain.errors import ErrorCode, RepoForgeError
from repoforge.domain.execution_plan import (
    EXECUTION_PLAN_SCHEMA_VERSION,
    ExecutionPlanBinding,
    ExecutionPlanState,
    PlanStage,
    PlanStageBoundary,
    PlanStageKind,
    PlanStageMutability,
    StageFailurePolicy,
    create_execution_plan,
    plan_payload,
    validate_plan_current,
)


def _stage(
    stage_id: str,
    *,
    kind: PlanStageKind = PlanStageKind.PROFILE,
    target: str = "quick",
    dependencies: tuple[str, ...] = (),
    boundary: PlanStageBoundary = PlanStageBoundary.ITERATION,
    mutability: PlanStageMutability = PlanStageMutability.READ_ONLY,
    failure_policy: StageFailurePolicy = StageFailurePolicy.REQUIRED,
) -> PlanStage:
    return PlanStage(
        stage_id=stage_id,
        kind=kind,
        target=target,
        selector=None,
        dependencies=dependencies,
        boundary=boundary,
        working_directory=None,
        timeout_seconds=120,
        mutability=mutability,
        network_policy="local_only",
        failure_policy=failure_policy,
        artifact_paths=(),
    )


def _plan(*, stages: tuple[PlanStage, ...] | None = None):
    selected = stages or (
        _stage("quick"),
        _stage(
            "full",
            target="full",
            dependencies=("quick",),
            boundary=PlanStageBoundary.FINAL,
            mutability=PlanStageMutability.WORKSPACE_WRITE,
        ),
    )
    return create_execution_plan(
        task_id="task-1",
        workspace_id="workspace-1",
        binding=ExecutionPlanBinding(
            head_sha="a" * 40,
            workspace_fingerprint="b" * 64,
            config_generation="c" * 64,
            policy_hash="d" * 64,
            assessment_snapshot_id="e" * 64,
            evidence_snapshot_ids=("f" * 64,),
            risk_assessment_hash="1" * 64,
            recommendation_hash="2" * 64,
        ),
        ordered_stages=selected,
        final_profile="full",
        created_at="2026-07-17T00:00:00+00:00",
        expires_at="2026-07-18T00:00:00+00:00",
    )


def test_execution_plan_is_deterministic_and_content_addressed() -> None:
    first = _plan()
    second = _plan()

    assert first == second
    assert first.plan_id.startswith("plan-")
    assert len(first.plan_hash) == 64
    assert first.schema_version == EXECUTION_PLAN_SCHEMA_VERSION
    assert json.dumps(plan_payload(first), sort_keys=True) == json.dumps(
        plan_payload(second), sort_keys=True
    )
    assert first.ordered_stages[-1].boundary is PlanStageBoundary.FINAL
    assert first.ordered_stages[-1].target == first.final_profile


def test_execution_plan_rejects_cycles_unknown_dependencies_and_missing_final_gate() -> None:
    with pytest.raises(RepoForgeError) as cycle:
        _plan(
            stages=(
                _stage("a", dependencies=("b",)),
                _stage("b", dependencies=("a",)),
            )
        )
    assert cycle.value.code is ErrorCode.STATE_INVALID

    with pytest.raises(RepoForgeError) as unknown:
        _plan(stages=(_stage("a", dependencies=("missing",)),))
    assert unknown.value.code is ErrorCode.STATE_INVALID

    with pytest.raises(RepoForgeError) as missing_final:
        _plan(stages=(_stage("quick"),))
    assert missing_final.value.code is ErrorCode.STATE_INVALID


def test_execution_plan_stale_matrix_reports_exact_reason() -> None:
    plan = _plan()
    current = ExecutionPlanState(
        head_sha=plan.binding.head_sha,
        workspace_fingerprint=plan.binding.workspace_fingerprint,
        config_generation=plan.binding.config_generation,
        policy_hash=plan.binding.policy_hash,
        risk_assessment_hash=plan.binding.risk_assessment_hash,
        recommendation_hash=plan.binding.recommendation_hash,
        stage_definition_hash=plan.stage_definition_hash,
        now="2026-07-17T12:00:00+00:00",
    )
    assert validate_plan_current(plan, current) == ()

    cases = {
        "head_sha": "head_sha",
        "workspace_fingerprint": "workspace_fingerprint",
        "config_generation": "config_generation",
        "policy_hash": "policy_hash",
        "risk_assessment_hash": "risk_assessment",
        "recommendation_hash": "recommendation",
        "stage_definition_hash": "stage_definition",
    }
    for field, expected in cases.items():
        stale = replace(current, **{field: "9" * (40 if field == "head_sha" else 64)})
        assert validate_plan_current(plan, stale) == (expected,)

    expired = replace(current, now="2026-07-19T00:00:00+00:00")
    assert validate_plan_current(plan, expired) == ("expired",)


def test_execution_plan_store_is_private_restart_safe_and_immutable(tmp_path: Path) -> None:
    locks = FcntlLockManager(tmp_path / "locks")
    store = JsonExecutionPlanStore(tmp_path, locks)
    plan = _plan()

    created = store.create(plan)
    assert created.value == plan
    assert store.read(plan.plan_id) == created
    mode = os.stat(store.root / f"{plan.plan_id}.json").st_mode & 0o777
    assert mode == 0o600

    restarted = JsonExecutionPlanStore(tmp_path, locks)
    assert restarted.read(plan.plan_id) == created
    with pytest.raises(RepoForgeError) as duplicate:
        restarted.create(replace(plan, final_profile="other"))
    assert duplicate.value.code is ErrorCode.ALREADY_EXISTS


def test_execution_plan_store_rejects_corrupt_and_future_schema(tmp_path: Path) -> None:
    locks = FcntlLockManager(tmp_path / "locks")
    store = JsonExecutionPlanStore(tmp_path, locks)
    plan = _plan()
    store.create(plan)
    path = store.root / f"{plan.plan_id}.json"

    path.write_text("not-json", encoding="utf-8")
    with pytest.raises(RepoForgeError) as corrupt:
        store.read(plan.plan_id)
    assert corrupt.value.code is ErrorCode.STATE_CORRUPT

    store = JsonExecutionPlanStore(tmp_path / "future", locks)
    store.create(plan)
    future = store.root / f"{plan.plan_id}.json"
    raw = json.loads(future.read_text(encoding="utf-8"))
    raw["schema_version"] = EXECUTION_PLAN_SCHEMA_VERSION + 1
    future.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(RepoForgeError) as unsupported:
        store.read(plan.plan_id)
    assert unsupported.value.code is ErrorCode.STATE_SCHEMA_UNSUPPORTED


def test_execution_plan_acceptance_binds_exact_plan_hash(tmp_path: Path) -> None:
    locks = FcntlLockManager(tmp_path / "locks")
    plans = JsonExecutionPlanStore(tmp_path, locks)
    acceptances = JsonExecutionPlanAcceptanceStore(tmp_path, locks)
    plan = plans.create(_plan()).value

    accepted = acceptances.accept(
        plan,
        acceptance_id="accept-1",
        task_id="task-1",
        accepted_at="2026-07-17T01:00:00+00:00",
    )
    assert accepted.value.plan_id == plan.plan_id
    assert accepted.value.plan_hash == plan.plan_hash
    assert acceptances.read_for_plan(plan.plan_id) == accepted


def test_service_creates_and_accepts_current_execution_plan(forge_env: ForgeEnvironment) -> None:
    workspace_id = forge_env.service.workspace_create("demo", "execution plan")["workspace_id"]
    current = forge_env.service.workspace_read_file(workspace_id, "hello.txt")
    forge_env.service.workspace_write_file(
        workspace_id,
        "hello.txt",
        "changed for plan\n",
        current["sha256"],
    )

    created = forge_env.service.workspace_create_execution_plan(
        workspace_id,
        task_id="task-1",
    )
    assert created["workspace_id"] == workspace_id
    assert created["final_profile"] == "full"
    assert created["ordered_stages"][-1]["target"] == "full"
    assert created["accepted"] is False

    accepted = forge_env.service.workspace_accept_execution_plan(
        workspace_id,
        created["plan_id"],
        task_id="task-1",
    )
    assert accepted["plan_id"] == created["plan_id"]
    assert accepted["plan_hash"] == created["plan_hash"]
    assert accepted["accepted"] is True

    record = forge_env.service.application.context.store.load(workspace_id)
    assert record.metadata["accepted_plan_id"] == created["plan_id"]
    assert record.metadata["plan_receipt"] == created["plan_hash"]


def test_service_rejects_acceptance_after_workspace_mutation(forge_env: ForgeEnvironment) -> None:
    workspace_id = forge_env.service.workspace_create("demo", "stale execution plan")[
        "workspace_id"
    ]
    created = forge_env.service.workspace_create_execution_plan(workspace_id)
    current = forge_env.service.workspace_read_file(workspace_id, "hello.txt")
    forge_env.service.workspace_write_file(
        workspace_id,
        "hello.txt",
        "mutated after plan\n",
        current["sha256"],
    )

    with pytest.raises(RepoForgeError) as stale:
        forge_env.service.workspace_accept_execution_plan(workspace_id, created["plan_id"])
    assert stale.value.code is ErrorCode.STATE_STALE
    assert "workspace_fingerprint" in stale.value.details["stale_reasons"]
