from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from conftest import ForgeEnvironment

from repoforge.adapters.locking import FcntlLockManager
from repoforge.adapters.persistence.json_iteration_cache import JsonIterationCache
from repoforge.application.service import CodingService
from repoforge.bootstrap import AdapterOverrides, build_application
from repoforge.config import load_config
from repoforge.domain.errors import ErrorCode, RepoForgeError
from repoforge.domain.execution_plan import (
    ExecutionPlanBinding,
    PlanStage,
    PlanStageBoundary,
    PlanStageKind,
    PlanStageMutability,
    StageFailurePolicy,
    create_execution_plan,
)
from repoforge.domain.execution_receipt import ArtifactDigest
from repoforge.domain.verification_dag import (
    CacheMissReason,
    CachePolicy,
    DagFailureSeverity,
    VerificationDagStage,
    build_iteration_cache_key,
    compile_plan_dag,
    create_iteration_cache_entry,
    create_verification_dag,
)
from repoforge.testing.fakes import ManualBackgroundTaskRunner


def _plan_stage(
    stage_id: str,
    *,
    dependencies: tuple[str, ...] = (),
    boundary: PlanStageBoundary = PlanStageBoundary.ITERATION,
    mutability: PlanStageMutability = PlanStageMutability.READ_ONLY,
    target: str = "cache-smoke",
) -> PlanStage:
    return PlanStage(
        stage_id=stage_id,
        kind=PlanStageKind.DIAGNOSTIC,
        target=target,
        selector=None,
        dependencies=dependencies,
        boundary=boundary,
        working_directory=None,
        timeout_seconds=30,
        mutability=mutability,
        network_policy="local_only",
        failure_policy=StageFailurePolicy.REQUIRED,
        artifact_paths=(),
    )


def _plan(*, stages: tuple[PlanStage, ...] | None = None):
    selected = stages or (
        _plan_stage("narrow"),
        PlanStage(
            stage_id="final",
            kind=PlanStageKind.PROFILE,
            target="full",
            selector=None,
            dependencies=("narrow",),
            boundary=PlanStageBoundary.FINAL,
            working_directory=None,
            timeout_seconds=300,
            mutability=PlanStageMutability.WORKSPACE_WRITE,
            network_policy="local_only",
            failure_policy=StageFailurePolicy.REQUIRED,
            artifact_paths=(),
        ),
    )
    return create_execution_plan(
        task_id="task-cache",
        workspace_id="workspace-cache",
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
    )


def _dag_stage(
    stage_id: str,
    *,
    dependencies: tuple[str, ...] = (),
    final: bool = False,
    mutability: str = "read_only",
    cache_policy: CachePolicy = CachePolicy.READ_ONLY,
) -> VerificationDagStage:
    return VerificationDagStage(
        stage_id=stage_id,
        version=1,
        dependencies=dependencies,
        kind="profile" if final else "diagnostic",
        target="full" if final else stage_id,
        selector=None,
        environment_adapter="native",
        working_directory=".",
        timeout_seconds=60,
        network_policy="local_only",
        filesystem_policy="workspace_write" if mutability == "workspace_write" else "read_only",
        process_policy="bounded_subprocess",
        credential_policy="none",
        mutability=mutability,
        required_risk_level="unknown",
        artifact_paths=(),
        cache_policy=CachePolicy.DISABLED if final else cache_policy,
        failure_severity=DagFailureSeverity.REQUIRED,
        final=final,
    )


def _cache_key(stage: VerificationDagStage, **overrides: object):
    values: dict[str, object] = {
        "workspace_identity": "a" * 64,
        "declared_input_hash": "b" * 64,
        "stage_definition_hash": stage.definition_hash,
        "target_identity": "c" * 64,
        "working_directory": ".",
        "environment_identity": "d" * 64,
        "environment_identity_schema_version": 2,
        "requested_policy_hash": "5" * 64,
        "effective_policy_hash": "6" * 64,
        "toolchain_hash": "e" * 64,
        "lockfile_hash": "f" * 64,
        "config_generation": "1" * 64,
        "policy_hash": "2" * 64,
        "provider_hash": "3" * 64,
        "network_policy": "local_only",
        "dependency_receipt_hashes": ("4" * 64,),
    }
    values.update(overrides)
    return build_iteration_cache_key(**values)


def test_linear_plan_compiles_to_deterministic_typed_dag() -> None:
    plan = _plan()

    first = compile_plan_dag(plan)
    second = compile_plan_dag(plan)

    assert first == second
    assert first.plan_id == plan.plan_id
    assert first.final_stage_id == "final"
    assert [stage.stage_id for stage in first.stages] == ["narrow", "final"]
    assert first.stages[0].cache_policy is CachePolicy.READ_ONLY
    assert first.stages[1].cache_policy is CachePolicy.DISABLED
    assert first.stages[1].final is True
    assert len(first.dag_hash) == 64
    assert all(len(stage.definition_hash) == 64 for stage in first.stages)


def test_dag_validation_rejects_cycles_unknown_dependencies_and_unsafe_cache_policy() -> None:
    with pytest.raises(RepoForgeError) as unknown:
        create_verification_dag(
            plan_id="plan-" + "a" * 24,
            plan_hash="b" * 64,
            stages=(
                _dag_stage("a", dependencies=("missing",)),
                _dag_stage("final", dependencies=("a",), final=True),
            ),
            final_stage_id="final",
        )
    assert unknown.value.code is ErrorCode.STATE_INVALID

    with pytest.raises(RepoForgeError) as cycle:
        create_verification_dag(
            plan_id="plan-" + "a" * 24,
            plan_hash="b" * 64,
            stages=(
                _dag_stage("a", dependencies=("b",)),
                _dag_stage("b", dependencies=("a",)),
                _dag_stage("final", dependencies=("b",), final=True),
            ),
            final_stage_id="final",
        )
    assert cycle.value.code is ErrorCode.STATE_INVALID

    with pytest.raises(RepoForgeError) as mutating_cache:
        _dag_stage("write", mutability="workspace_write", cache_policy=CachePolicy.READ_ONLY)
    assert mutating_cache.value.code is ErrorCode.STATE_INVALID

    with pytest.raises(RepoForgeError) as missing_final:
        create_verification_dag(
            plan_id="plan-" + "a" * 24,
            plan_hash="b" * 64,
            stages=(_dag_stage("a"),),
            final_stage_id="missing",
        )
    assert missing_final.value.code is ErrorCode.STATE_INVALID


def test_cache_key_changes_for_every_compatibility_dimension() -> None:
    stage = _dag_stage("cache")
    baseline = _cache_key(stage)
    cases = {
        "workspace_identity": "9" * 64,
        "declared_input_hash": "8" * 64,
        "stage_definition_hash": "7" * 64,
        "target_identity": "6" * 64,
        "working_directory": "src",
        "environment_identity": "5" * 64,
        "environment_identity_schema_version": 3,
        "requested_policy_hash": "7" * 64,
        "effective_policy_hash": "8" * 64,
        "toolchain_hash": "0" * 64,
        "lockfile_hash": "a" * 64,
        "config_generation": "b" * 64,
        "policy_hash": "c" * 64,
        "provider_hash": "d" * 64,
        "network_policy": "none",
        "dependency_receipt_hashes": ("e" * 64,),
    }
    for field, value in cases.items():
        changed = _cache_key(stage, **{field: value})
        assert changed.cache_key != baseline.cache_key, field
    assert _cache_key(stage) == baseline


def _write_legacy_v1_cache_record(
    store: JsonIterationCache,
    current_key: object,
    *,
    stage_definition_hash: str | None = None,
) -> None:
    key = current_key
    legacy_key = {
        "cache_key": "9" * 64,
        "workspace_identity": key.workspace_identity,
        "declared_input_hash": key.declared_input_hash,
        "stage_definition_hash": stage_definition_hash or key.stage_definition_hash,
        "target_identity": key.target_identity,
        "working_directory": key.working_directory,
        "environment_identity": "0" * 64,
        "toolchain_hash": key.toolchain_hash,
        "lockfile_hash": key.lockfile_hash,
        "config_generation": key.config_generation,
        "policy_hash": key.policy_hash,
        "provider_hash": key.provider_hash,
        "network_policy": key.network_policy,
        "dependency_receipt_hashes": list(key.dependency_receipt_hashes),
        "schema_version": 1,
    }
    record_id = "cache-" + "9" * 24
    envelope = {
        "payload": {
            "artifact_digests": [],
            "created_at": "2026-07-17T00:00:00+00:00",
            "entry_id": record_id,
            "key": legacy_key,
            "schema_version": 1,
            "source_receipt_id": "receipt-" + "8" * 24,
        },
        "record_id": record_id,
        "revision": 1,
        "schema_version": 1,
    }
    (store.root / f"{record_id}.json").write_text(json.dumps(envelope), encoding="utf-8")


def test_legacy_v1_cache_reports_environment_schema_change_only_when_compatible(
    tmp_path: Path,
) -> None:
    store = JsonIterationCache(tmp_path / "state", FcntlLockManager(tmp_path / "locks"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    key = _cache_key(_dag_stage("cache"))
    _write_legacy_v1_cache_record(store, key)

    compatible = store.lookup(key, workspace_root=workspace)

    assert compatible.hit is False
    assert compatible.reason is CacheMissReason.ENVIRONMENT_IDENTITY_SCHEMA_CHANGED

    unrelated = JsonIterationCache(
        tmp_path / "other-state", FcntlLockManager(tmp_path / "other-locks")
    )
    _write_legacy_v1_cache_record(
        unrelated,
        key,
        stage_definition_hash="7" * 64,
    )
    miss = unrelated.lookup(key, workspace_root=workspace)
    assert miss.hit is False
    assert miss.reason is CacheMissReason.NOT_FOUND


def test_iteration_cache_hit_miss_artifact_integrity_corruption_and_eviction(
    tmp_path: Path,
) -> None:
    locks = FcntlLockManager(tmp_path / "locks")
    store = JsonIterationCache(tmp_path / "state", locks, max_entries=2)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    artifact = workspace / "report.json"
    artifact.write_text('{"ok": true}\n', encoding="utf-8")
    artifact_digest = ArtifactDigest(
        "report.json",
        __import__("hashlib").sha256(artifact.read_bytes()).hexdigest(),
    )
    stage = _dag_stage("cache")
    key = _cache_key(stage)
    entry = create_iteration_cache_entry(
        key=key,
        source_receipt_id="receipt-" + "a" * 24,
        artifact_digests=(artifact_digest,),
        created_at="2026-07-17T00:00:00+00:00",
    )

    missing = store.lookup(key, workspace_root=workspace)
    assert missing.hit is False
    assert missing.reason is CacheMissReason.NOT_FOUND

    store.put(entry)
    hit = store.lookup(key, workspace_root=workspace)
    assert hit.hit is True
    assert hit.entry == entry

    artifact.write_text("changed\n", encoding="utf-8")
    mismatch = store.lookup(key, workspace_root=workspace)
    assert mismatch.hit is False
    assert mismatch.reason is CacheMissReason.ARTIFACT_MISMATCH

    artifact.unlink()
    absent = store.lookup(key, workspace_root=workspace)
    assert absent.reason is CacheMissReason.ARTIFACT_MISSING

    cache_file = store.root / f"{entry.entry_id}.json"
    cache_file.write_text("not-json", encoding="utf-8")
    corrupt = store.lookup(key, workspace_root=workspace)
    assert corrupt.hit is False
    assert corrupt.reason is CacheMissReason.CORRUPT

    empty_artifacts: tuple[ArtifactDigest, ...] = ()
    entries = []
    for index in range(3):
        candidate_key = _cache_key(stage, workspace_identity=f"{index + 1:x}" * 64)
        candidate = create_iteration_cache_entry(
            key=candidate_key,
            source_receipt_id=f"receipt-{index + 1:024x}",
            artifact_digests=empty_artifacts,
            created_at=f"2026-07-17T00:00:0{index}+00:00",
        )
        store.put(candidate, protected_entry_ids={entries[0].entry_id} if entries else set())
        entries.append(candidate)
    assert store.read(entries[0].entry_id) is not None
    assert store.read(entries[1].entry_id) is None
    assert store.read(entries[2].entry_id) is not None


def test_iteration_cache_concurrent_same_entry_is_idempotent(tmp_path: Path) -> None:
    locks = FcntlLockManager(tmp_path / "locks")
    store = JsonIterationCache(tmp_path / "state", locks, max_entries=10)
    stage = _dag_stage("concurrent")
    entry = create_iteration_cache_entry(
        key=_cache_key(stage),
        source_receipt_id="receipt-" + "a" * 24,
        artifact_digests=(),
        created_at="2026-07-17T00:00:00+00:00",
    )

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: store.put(entry), range(32)))

    assert results == [entry] * 32
    assert store.read(entry.entry_id) == entry
    assert len(list(store.root.glob("cache-*.json"))) == 1


def _cache_service(env: ForgeEnvironment) -> tuple[CodingService, ManualBackgroundTaskRunner]:
    text = env.config_path.read_text(encoding="utf-8")
    text += """

[repositories.demo.diagnostics.cache-smoke]
summary = "Cacheable no-selector diagnostic"
argv = ["python3", "-c", "print('1 passed in 0.01s')"]
selector_kind = "none"
timeout_seconds = 30
network_policy = "local_only"
mutability = "read_only"
parser = "pytest"
output_limit = 2000

[repositories.demo.risk]
narrow_diagnostics = ["cache-smoke"]
ordered_profiles = ["full"]
final_profile = "full"
"""
    env.config_path.write_text(text, encoding="utf-8")
    runner = ManualBackgroundTaskRunner()
    config = load_config(env.config_path)
    app = build_application(config, overrides=AdapterOverrides(background_tasks=runner))
    return CodingService(config, application=app), runner


def _audit_count(env: ForgeEnvironment, action: str) -> int:
    path = env.root / "state" / "audit.jsonl"
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    return sum(record.get("action") == action for record in records)


def test_plan_executor_reuses_only_read_only_iteration_stage_and_always_runs_final(
    forge_env: ForgeEnvironment,
) -> None:
    service, runner = _cache_service(forge_env)
    workspace_id = service.workspace_create("demo", "cache integration")["workspace_id"]
    current = service.workspace_read_file(workspace_id, "hello.txt")
    service.workspace_write_file(
        workspace_id,
        "hello.txt",
        "changed for cache integration\n",
        current["sha256"],
    )
    plan = service.workspace_create_execution_plan(workspace_id, task_id="task-cache-integration")
    service.workspace_accept_execution_plan(
        workspace_id,
        plan["plan_id"],
        task_id="task-cache-integration",
    )
    assert [stage["target"] for stage in plan["ordered_stages"]] == ["cache-smoke", "full"]

    first = service.workspace_execute_plan(workspace_id, plan["plan_id"], through="iteration")
    runner.run(first["operation_id"])
    first_result = service.operation_status(first["operation_id"])["result"]
    first_receipt = first_result["stage_receipts"][0]
    assert first_receipt["cache_status"] == "miss"
    assert first_receipt["schema_version"] == 2
    assert first_receipt["environment_identity_schema_version"] == 2
    assert len(first_receipt["requested_policy_hash"]) == 64
    assert len(first_receipt["effective_policy_hash"]) == 64
    diagnostic_runs = _audit_count(forge_env, "workspace_run_diagnostic")

    second = service.workspace_execute_plan(workspace_id, plan["plan_id"], through="iteration")
    runner.run(second["operation_id"])
    second_result = service.operation_status(second["operation_id"])["result"]
    assert second_result["stage_receipts"][0]["cache_status"] == "hit"
    assert (
        second_result["stage_receipts"][0]["receipt_id"]
        != first_result["stage_receipts"][0]["receipt_id"]
    )
    assert _audit_count(forge_env, "workspace_run_diagnostic") == diagnostic_runs
    assert service.workspace_status(workspace_id)["last_verification"] is None

    full = service.workspace_execute_plan(workspace_id, plan["plan_id"], through="full")
    runner.run(full["operation_id"])
    full_result = service.operation_status(full["operation_id"])["result"]
    assert [receipt["cache_status"] for receipt in full_result["stage_receipts"]] == [
        "hit",
        "not_cacheable",
    ]
    assert full_result["satisfies_commit_gate"] is True
    assert service.workspace_status(workspace_id)["last_verification"]["profile"] == "full"
