from __future__ import annotations

import importlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import pytest

from repoforge.adapters.persistence.json_state_lifecycle import JsonStateLifecycleManager
from repoforge.adapters.persistence.json_state_recovery import JsonStateRecoveryManager
from repoforge.adapters.persistence.json_state_repository import JsonStateRepository
from repoforge.domain.durable_state import Revision, SchemaVersion, StateEnvelope
from repoforge.domain.errors import ErrorCode, RepoForgeError
from repoforge.domain.state_lifecycle import (
    CleanupDisposition,
    IntegritySeverity,
    MigrationDirection,
    StateMigrationRegistry,
    StateMigrationStep,
    StateProtection,
    StateRecordReference,
    StateRetentionPolicy,
)
from repoforge.testing.fakes import (
    FixedClock,
    InMemoryLockManager,
    SequenceIdGenerator,
)


@dataclass(frozen=True, slots=True)
class DemoRecord:
    name: str


class DemoCodec:
    schema_version = SchemaVersion(1)

    def encode(self, value: DemoRecord) -> dict[str, object]:
        return {"name": value.name}

    def decode(self, payload: dict[str, object]) -> DemoRecord:
        if set(payload) != {"name"} or not isinstance(payload["name"], str):
            raise ValueError("invalid demo record")
        return DemoRecord(payload["name"])


def _store(tmp_path: Path, *, max_record_bytes: int = 4096) -> JsonStateRepository[DemoRecord]:
    return JsonStateRepository(
        tmp_path,
        collection="demo_records",
        locks=InMemoryLockManager(),
        codec=DemoCodec(),
        id_validator=lambda value: (
            value if value.startswith("demo-") else (_ for _ in ()).throw(ValueError("bad id"))
        ),
        max_record_bytes=max_record_bytes,
    )


def test_state_envelope_types_are_positive_and_deterministic() -> None:
    envelope = StateEnvelope("demo-1", SchemaVersion(1), Revision(1), DemoRecord("alpha"))
    assert envelope.schema_version.value == 1
    assert envelope.revision.value == 1
    with pytest.raises(ValueError):
        SchemaVersion(0)
    with pytest.raises(ValueError):
        Revision(0)


def test_json_state_repository_is_private_atomic_restart_safe_and_cas(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    created = store.create("demo-1", DemoRecord("alpha"))
    assert created.revision == Revision(1)
    path = tmp_path / "demo_records" / "demo-1.json"
    assert os.stat(path.parent).st_mode & 0o777 == 0o700
    assert os.stat(path).st_mode & 0o777 == 0o600
    assert store.read("demo-1") == created
    assert _store(tmp_path).read("demo-1") == created
    assert not list(path.parent.glob("*.tmp-*"))

    saved = store.save("demo-1", DemoRecord("beta"), expected_revision=Revision(1))
    assert saved.revision == Revision(2)
    with pytest.raises(RepoForgeError) as stale:
        store.save("demo-1", DemoRecord("lost"), expected_revision=Revision(1))
    assert stale.value.code is ErrorCode.STATE_STALE
    assert store.read("demo-1") == saved


def test_json_state_repository_rejects_corruption_future_schema_identity_and_size(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    store.create("demo-1", DemoRecord("alpha"))
    path = tmp_path / "demo_records" / "demo-1.json"

    path.write_text("{bad", encoding="utf-8")
    with pytest.raises(RepoForgeError) as corrupt:
        store.read("demo-1")
    assert corrupt.value.code is ErrorCode.STATE_CORRUPT

    path.write_text(
        json.dumps(
            {
                "record_id": "demo-1",
                "schema_version": 99,
                "revision": 1,
                "payload": {"name": "alpha"},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(RepoForgeError) as future:
        store.read("demo-1")
    assert future.value.code is ErrorCode.STATE_SCHEMA_UNSUPPORTED

    path.write_text(
        json.dumps(
            {
                "record_id": "demo-2",
                "schema_version": 1,
                "revision": 1,
                "payload": {"name": "alpha"},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(RepoForgeError) as mismatch:
        store.read("demo-1")
    assert mismatch.value.code is ErrorCode.STATE_CORRUPT

    tiny = _store(tmp_path / "tiny", max_record_bytes=80)
    with pytest.raises(RepoForgeError) as too_large:
        tiny.create("demo-1", DemoRecord("x" * 200))
    assert too_large.value.code is ErrorCode.STATE_TOO_LARGE


def test_task_capsule_transitions_require_disposed_criteria_and_compact_resume() -> None:
    task_module = importlib.import_module("repoforge.domain.task_capsule")
    criterion = task_module.TaskCriterion(
        "criterion-1", "The exact requested behavior is verified."
    )
    task = task_module.TaskCapsule(
        task_id="task-0123456789abcdef01234567",
        state=task_module.TaskState.DRAFT,
        intent="Implement a resumable durable task contract.",
        acceptance_criteria=(criterion,),
        constraints=("Do not persist source bodies.",),
        repo_ids=("repoforge",),
        workspace_bindings=(),
        source_issue_or_pr="#18",
        active_config_generation=3,
        accepted_plan_id=None,
        decisions=(),
        evidence_snapshot_ids=(),
        receipt_ids=(),
        current_phase="design",
        blocked_reason=None,
        open_questions=(task_module.TaskQuestion("question-1", "Which public slice follows?"),),
        next_safe_actions=(
            task_module.TaskAction("implement_domain", "The contract is approved.", True),
        ),
        created_at="2026-07-16T00:00:00+00:00",
        updated_at="2026-07-16T00:00:00+00:00",
    )

    ready = task_module.transition_task(
        task,
        task_module.TaskState.READY,
        updated_at="2026-07-16T00:01:00+00:00",
    )
    active = task_module.transition_task(
        ready,
        task_module.TaskState.ACTIVE,
        updated_at="2026-07-16T00:02:00+00:00",
    )
    with pytest.raises(ValueError, match="acceptance criteria"):
        task_module.transition_task(
            active,
            task_module.TaskState.COMPLETED,
            updated_at="2026-07-16T00:03:00+00:00",
        )

    completed = task_module.transition_task(
        task_module.replace_task(
            active,
            acceptance_criteria=(
                task_module.TaskCriterion(
                    criterion.criterion_id,
                    criterion.summary,
                    task_module.CriterionStatus.PASSED,
                    ("evidence-1",),
                ),
            ),
            open_questions=(),
            current_phase="final",
            updated_at="2026-07-16T00:03:00+00:00",
        ),
        task_module.TaskState.COMPLETED,
        updated_at="2026-07-16T00:04:00+00:00",
    )

    projection = completed.resume_projection()
    assert projection["task_id"] == completed.task_id
    assert projection["state"] == "completed"
    assert projection["criteria"] == {"passed": 1}
    assert projection["next_safe_actions"][0]["action"] == "implement_domain"
    rendered = json.dumps(projection, sort_keys=True)
    assert "source bodies" not in rendered.lower()
    with pytest.raises(ValueError, match="terminal"):
        task_module.transition_task(
            completed,
            task_module.TaskState.ACTIVE,
            updated_at="2026-07-16T00:05:00+00:00",
        )


def test_json_task_store_is_private_restart_safe_and_revision_bound(tmp_path: Path) -> None:
    task_module = importlib.import_module("repoforge.domain.task_capsule")
    store_module = importlib.import_module("repoforge.adapters.persistence.json_task_store")
    store = store_module.JsonTaskStore(tmp_path, InMemoryLockManager())
    task = task_module.TaskCapsule.new(
        task_id="task-0123456789abcdef01234567",
        intent="Persist task state independently of chat history.",
        acceptance_criteria=("Task survives restart.",),
        constraints=("Store summaries and identifiers only.",),
        repo_ids=("repoforge",),
        created_at="2026-07-16T00:00:00+00:00",
    )

    created = store.create(task)
    assert created.revision == Revision(1)
    path = tmp_path / "task-capsules" / f"{task.task_id}.json"
    assert os.stat(path.parent).st_mode & 0o777 == 0o700
    assert os.stat(path).st_mode & 0o777 == 0o600
    assert store_module.JsonTaskStore(tmp_path, InMemoryLockManager()).read(task.task_id) == created

    updated = task_module.replace_task(
        task,
        current_phase="implementation",
        updated_at="2026-07-16T00:01:00+00:00",
    )
    saved = store.save(updated, expected_revision=Revision(1))
    assert saved.revision == Revision(2)
    with pytest.raises(RepoForgeError) as stale:
        store.save(updated, expected_revision=Revision(1))
    assert stale.value.code is ErrorCode.STATE_STALE

    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["schema_version"] = 99
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(RepoForgeError) as future:
        store.read(task.task_id)
    assert future.value.code is ErrorCode.STATE_SCHEMA_UNSUPPORTED


def test_task_capsule_service_creates_and_resumes_revision_bound_tasks(tmp_path: Path) -> None:
    service_module = importlib.import_module("repoforge.application.tasks")
    store_module = importlib.import_module("repoforge.adapters.persistence.json_task_store")
    task_module = importlib.import_module("repoforge.domain.task_capsule")
    service = service_module.TaskCapsuleService(
        store=store_module.JsonTaskStore(tmp_path, InMemoryLockManager()),
        clock=FixedClock("2026-07-16T00:00:00+00:00"),
        ids=SequenceIdGenerator(("0123456789abcdef01234567",)),
    )

    created = service.create(
        intent="Resume without reconstructing state from chat.",
        acceptance_criteria=("The task is persisted.",),
        repo_ids=("repoforge",),
    )

    assert created.value.task_id == "task-0123456789abcdef01234567"
    assert service.resume(created.value.task_id)["revision"] == 1
    ready = service.transition(
        created.value.task_id,
        task_module.TaskState.READY,
        expected_revision=Revision(1),
    )
    assert ready.revision == Revision(2)
    assert ready.value.state is task_module.TaskState.READY


def test_json_state_repository_rejects_unsafe_collection_and_record_ids(
    tmp_path: Path,
) -> None:
    with pytest.raises(RepoForgeError):
        JsonStateRepository(
            tmp_path,
            collection="../escape",
            locks=InMemoryLockManager(),
            codec=DemoCodec(),
            id_validator=lambda value: value,
        )
    store = _store(tmp_path)
    with pytest.raises(RepoForgeError):
        store.read("../escape")


def _add_display_name(payload: dict[str, object]) -> dict[str, object]:
    migrated = dict(payload)
    migrated["display_name"] = migrated.pop("name")
    return migrated


def _remove_display_name(payload: dict[str, object]) -> dict[str, object]:
    migrated = dict(payload)
    migrated["name"] = migrated.pop("display_name")
    return migrated


def _add_enabled(payload: dict[str, object]) -> dict[str, object]:
    return {**payload, "enabled": True}


def _remove_enabled(payload: dict[str, object]) -> dict[str, object]:
    migrated = dict(payload)
    migrated.pop("enabled")
    return migrated


def _migration_registry() -> StateMigrationRegistry:
    return StateMigrationRegistry(
        (
            StateMigrationStep(
                collection="demo_records",
                from_version=SchemaVersion(1),
                to_version=SchemaVersion(2),
                forward=_add_display_name,
                reverse=_remove_display_name,
            ),
            StateMigrationStep(
                collection="demo_records",
                from_version=SchemaVersion(2),
                to_version=SchemaVersion(3),
                forward=_add_enabled,
                reverse=_remove_enabled,
            ),
        )
    )


def test_migration_registry_plans_noop_and_ordered_multi_step_paths() -> None:
    registry = _migration_registry()

    noop = registry.plan("demo_records", SchemaVersion(2), SchemaVersion(2))
    assert noop.direction is MigrationDirection.FORWARD
    assert noop.steps == ()
    assert len(noop.plan_digest) == 64

    plan = registry.plan("demo_records", SchemaVersion(1), SchemaVersion(3))
    assert plan.direction is MigrationDirection.FORWARD
    assert tuple((step.from_version.value, step.to_version.value) for step in plan.steps) == (
        (1, 2),
        (2, 3),
    )
    assert registry.migrate_payload(plan, {"name": "alpha"}) == {
        "display_name": "alpha",
        "enabled": True,
    }


def test_migration_registry_requires_explicit_reverse_steps() -> None:
    registry = _migration_registry()
    plan = registry.plan("demo_records", SchemaVersion(3), SchemaVersion(1))
    assert plan.direction is MigrationDirection.REVERSE
    assert tuple((step.from_version.value, step.to_version.value) for step in plan.steps) == (
        (2, 3),
        (1, 2),
    )
    assert registry.migrate_payload(
        plan,
        {"display_name": "alpha", "enabled": True},
    ) == {"name": "alpha"}

    one_way = StateMigrationRegistry(
        (
            StateMigrationStep(
                collection="one_way",
                from_version=SchemaVersion(1),
                to_version=SchemaVersion(2),
                forward=lambda payload: {**payload, "version": 2},
            ),
        )
    )
    with pytest.raises(RepoForgeError) as missing_reverse:
        one_way.plan("one_way", SchemaVersion(2), SchemaVersion(1))
    assert missing_reverse.value.code is ErrorCode.STATE_INVALID


def test_migration_registry_rejects_duplicate_gapped_and_future_paths() -> None:
    step = StateMigrationStep(
        collection="demo_records",
        from_version=SchemaVersion(1),
        to_version=SchemaVersion(2),
        forward=lambda payload: dict(payload),
    )
    with pytest.raises(RepoForgeError) as duplicate:
        StateMigrationRegistry((step, step))
    assert duplicate.value.code is ErrorCode.STATE_INVALID

    registry = StateMigrationRegistry((step,))
    with pytest.raises(RepoForgeError) as gap:
        registry.plan("demo_records", SchemaVersion(1), SchemaVersion(3))
    assert gap.value.code is ErrorCode.STATE_INVALID

    with pytest.raises(RepoForgeError) as future:
        registry.plan("demo_records", SchemaVersion(4), SchemaVersion(2))
    assert future.value.code is ErrorCode.STATE_SCHEMA_UNSUPPORTED

    with pytest.raises(RepoForgeError) as invalid_transform:
        registry.migrate_payload(
            registry.plan("demo_records", SchemaVersion(1), SchemaVersion(2)),
            cast(dict[str, object], {"unsupported": object()}),
        )
    assert invalid_transform.value.code is ErrorCode.STATE_INVALID


def _write_raw_state(
    root: Path,
    *,
    collection: str,
    record_id: str,
    version: int,
    revision: int,
    payload: dict[str, object],
) -> bytes:
    directory = root / collection
    directory.mkdir(parents=True, exist_ok=True)
    encoded = (
        json.dumps(
            {
                "payload": payload,
                "record_id": record_id,
                "revision": revision,
                "schema_version": version,
            },
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
        )
        + "\n"
    ).encode()
    (directory / f"{record_id}.json").write_bytes(encoded)
    return encoded


def _read_raw_state(root: Path, collection: str, record_id: str) -> dict[str, object]:
    return json.loads((root / collection / f"{record_id}.json").read_bytes())


def test_json_state_lifecycle_migrates_mixed_versions_with_backup_and_idempotency(
    tmp_path: Path,
) -> None:
    _write_raw_state(
        tmp_path,
        collection="demo_records",
        record_id="demo-1",
        version=1,
        revision=1,
        payload={"name": "alpha"},
    )
    _write_raw_state(
        tmp_path,
        collection="demo_records",
        record_id="demo-2",
        version=2,
        revision=4,
        payload={"display_name": "beta"},
    )
    backup_observed: list[bool] = []

    def assert_backup_precedes_write(phase: str, record_id: str, index: int) -> None:
        if phase == "before_migration_write":
            backup_observed.append(
                (
                    tmp_path / ".state-lifecycle" / "backups" / preview.plan_id / "manifest.json"
                ).is_file()
            )

    manager = JsonStateLifecycleManager(
        tmp_path,
        InMemoryLockManager(),
        fault_injector=assert_backup_precedes_write,
    )
    preview = manager.preview_migration(
        collection="demo_records",
        registry=_migration_registry(),
        target_version=SchemaVersion(3),
        max_records=10,
    )
    assert preview.migrated_records == 2
    assert preview.unchanged_records == 0
    assert tuple(item.record_id for item in preview.records) == ("demo-1", "demo-2")
    assert all(len(item.source_checksum) == 64 for item in preview.records)
    assert all(len(item.target_checksum) == 64 for item in preview.records)

    report = manager.apply_migration(preview, registry=_migration_registry())
    assert report.processed == 2
    assert report.migrated == 2
    assert report.unchanged == 0
    assert report.rolled_back is False
    assert backup_observed == [True, True]
    assert _read_raw_state(tmp_path, "demo_records", "demo-1") == {
        "payload": {"display_name": "alpha", "enabled": True},
        "record_id": "demo-1",
        "revision": 2,
        "schema_version": 3,
    }
    assert _read_raw_state(tmp_path, "demo_records", "demo-2") == {
        "payload": {"display_name": "beta", "enabled": True},
        "record_id": "demo-2",
        "revision": 5,
        "schema_version": 3,
    }

    repeated = manager.apply_migration(preview, registry=_migration_registry())
    assert repeated == report


def test_json_state_lifecycle_rejects_stale_and_corrupt_migration_previews(
    tmp_path: Path,
) -> None:
    _write_raw_state(
        tmp_path,
        collection="demo_records",
        record_id="demo-1",
        version=1,
        revision=1,
        payload={"name": "alpha"},
    )
    manager = JsonStateLifecycleManager(tmp_path, InMemoryLockManager())
    preview = manager.preview_migration(
        collection="demo_records",
        registry=_migration_registry(),
        target_version=SchemaVersion(3),
    )
    _write_raw_state(
        tmp_path,
        collection="demo_records",
        record_id="demo-1",
        version=1,
        revision=2,
        payload={"name": "changed"},
    )
    with pytest.raises(RepoForgeError) as stale:
        manager.apply_migration(preview, registry=_migration_registry())
    assert stale.value.code is ErrorCode.STATE_STALE

    (tmp_path / "demo_records" / "demo-1.json").write_text("{bad", encoding="utf-8")
    with pytest.raises(RepoForgeError) as corrupt:
        manager.preview_migration(
            collection="demo_records",
            registry=_migration_registry(),
            target_version=SchemaVersion(3),
        )
    assert corrupt.value.code is ErrorCode.STATE_CORRUPT


def test_json_state_lifecycle_rolls_back_failed_migration_exactly(tmp_path: Path) -> None:
    original_1 = _write_raw_state(
        tmp_path,
        collection="demo_records",
        record_id="demo-1",
        version=1,
        revision=1,
        payload={"name": "alpha"},
    )
    original_2 = _write_raw_state(
        tmp_path,
        collection="demo_records",
        record_id="demo-2",
        version=1,
        revision=1,
        payload={"name": "beta"},
    )

    def fail_second_write(phase: str, record_id: str, index: int) -> None:
        if phase == "before_migration_write" and index == 1:
            raise OSError("injected migration write failure")

    manager = JsonStateLifecycleManager(
        tmp_path,
        InMemoryLockManager(),
        fault_injector=fail_second_write,
    )
    preview = manager.preview_migration(
        collection="demo_records",
        registry=_migration_registry(),
        target_version=SchemaVersion(3),
    )
    with pytest.raises(RepoForgeError) as failed:
        manager.apply_migration(preview, registry=_migration_registry())
    assert failed.value.code is ErrorCode.STATE_PERSISTENCE_FAILED
    assert (tmp_path / "demo_records" / "demo-1.json").read_bytes() == original_1
    assert (tmp_path / "demo_records" / "demo-2.json").read_bytes() == original_2


def test_json_state_lifecycle_recovers_interrupted_migration_after_restart(
    tmp_path: Path,
) -> None:
    original_1 = _write_raw_state(
        tmp_path,
        collection="demo_records",
        record_id="demo-1",
        version=1,
        revision=1,
        payload={"name": "alpha"},
    )
    original_2 = _write_raw_state(
        tmp_path,
        collection="demo_records",
        record_id="demo-2",
        version=1,
        revision=1,
        payload={"name": "beta"},
    )

    def crash_second_write(phase: str, record_id: str, index: int) -> None:
        if phase == "before_migration_write" and index == 1:
            raise SystemExit("simulated process crash")

    manager = JsonStateLifecycleManager(
        tmp_path,
        InMemoryLockManager(),
        fault_injector=crash_second_write,
    )
    preview = manager.preview_migration(
        collection="demo_records",
        registry=_migration_registry(),
        target_version=SchemaVersion(3),
    )
    with pytest.raises(SystemExit):
        manager.apply_migration(preview, registry=_migration_registry())
    assert (tmp_path / "demo_records" / "demo-1.json").read_bytes() != original_1

    restarted = JsonStateLifecycleManager(tmp_path, InMemoryLockManager())
    assert restarted.recover_incomplete_migrations() == (preview.plan_id,)
    assert (tmp_path / "demo_records" / "demo-1.json").read_bytes() == original_1
    assert (tmp_path / "demo_records" / "demo-2.json").read_bytes() == original_2


def test_json_state_lifecycle_noop_preview_does_not_create_backup(tmp_path: Path) -> None:
    _write_raw_state(
        tmp_path,
        collection="demo_records",
        record_id="demo-1",
        version=3,
        revision=1,
        payload={"display_name": "alpha", "enabled": True},
    )
    manager = JsonStateLifecycleManager(tmp_path, InMemoryLockManager())
    preview = manager.preview_migration(
        collection="demo_records",
        registry=_migration_registry(),
        target_version=SchemaVersion(3),
    )
    assert preview.migrated_records == 0
    assert preview.unchanged_records == 1
    report = manager.apply_migration(preview, registry=_migration_registry())
    assert report.migrated == 0
    assert report.unchanged == 1
    assert not (tmp_path / ".state-lifecycle" / "backups" / preview.plan_id).exists()


def test_cleanup_preview_is_reference_aware_bounded_and_quota_driven(tmp_path: Path) -> None:
    for record_id, _created_at, padding in (
        ("demo-1", "2026-01-01T00:00:00+00:00", "x" * 60),
        ("demo-2", "2026-02-01T00:00:00+00:00", "x" * 50),
        ("demo-3", "2026-03-01T00:00:00+00:00", "x" * 40),
        ("demo-4", "2026-04-01T00:00:00+00:00", "x" * 30),
        ("demo-5", "2026-05-01T00:00:00+00:00", "x" * 20),
    ):
        _write_raw_state(
            tmp_path,
            collection="demo_records",
            record_id=record_id,
            version=3,
            revision=1,
            payload={"display_name": record_id, "enabled": True, "padding": padding},
        )
    manager = JsonStateLifecycleManager(tmp_path, InMemoryLockManager())
    preview = manager.preview_cleanup(
        collection="demo_records",
        policy=StateRetentionPolicy(
            now="2026-07-01T00:00:00+00:00",
            retention_seconds=60 * 60 * 24 * 120,
            max_records=3,
            max_total_bytes=500,
            batch_size=2,
        ),
        record_timestamps={
            "demo-1": "2026-01-01T00:00:00+00:00",
            "demo-2": "2026-02-01T00:00:00+00:00",
            "demo-3": "2026-03-01T00:00:00+00:00",
            "demo-4": "2026-04-01T00:00:00+00:00",
            "demo-5": "2026-05-01T00:00:00+00:00",
        },
        protections=(StateProtection("demo-1", "active_task"),),
        references=(
            StateRecordReference("demo-5", "demo-2", "accepted_plan"),
            StateRecordReference("demo-5", "missing-record", "receipt"),
        ),
    )
    assert preview.protected_record_ids == ("demo-1", "demo-2")
    assert preview.orphan_references == (("demo-5", "missing-record", "receipt"),)
    assert tuple(item.record_id for item in preview.candidates) == ("demo-3", "demo-4")
    assert preview.candidates[0].disposition is CleanupDisposition.EXPIRED
    assert preview.next_cursor == "demo-4"
    assert preview.remaining_candidate_count >= 1


def test_cleanup_apply_is_stale_safe_idempotent_and_moves_to_private_trash(
    tmp_path: Path,
) -> None:
    for index in range(1, 4):
        _write_raw_state(
            tmp_path,
            collection="demo_records",
            record_id=f"demo-{index}",
            version=3,
            revision=1,
            payload={"display_name": f"demo-{index}", "enabled": True},
        )
    manager = JsonStateLifecycleManager(tmp_path, InMemoryLockManager())
    preview = manager.preview_cleanup(
        collection="demo_records",
        policy=StateRetentionPolicy(
            now="2026-07-01T00:00:00+00:00",
            retention_seconds=1,
            max_records=10,
            max_total_bytes=10_000,
            batch_size=10,
        ),
        record_timestamps={
            "demo-1": "2026-01-01T00:00:00+00:00",
            "demo-2": "2026-01-02T00:00:00+00:00",
            "demo-3": "2026-01-03T00:00:00+00:00",
        },
        protections=(StateProtection("demo-3", "audit_required"),),
    )
    report = manager.apply_cleanup(preview)
    assert report.processed == 2
    assert report.deleted == 2
    assert report.protected == 1
    assert report.reclaimed_bytes > 0
    assert not (tmp_path / "demo_records" / "demo-1.json").exists()
    assert (tmp_path / ".state-lifecycle" / "trash" / preview.plan_id / "demo-1.json").is_file()
    assert manager.apply_cleanup(preview) == report


def test_cleanup_apply_rejects_concurrent_changes(tmp_path: Path) -> None:
    _write_raw_state(
        tmp_path,
        collection="demo_records",
        record_id="demo-1",
        version=3,
        revision=1,
        payload={"display_name": "alpha", "enabled": True},
    )
    manager = JsonStateLifecycleManager(tmp_path, InMemoryLockManager())
    preview = manager.preview_cleanup(
        collection="demo_records",
        policy=StateRetentionPolicy(
            now="2026-07-01T00:00:00+00:00",
            retention_seconds=1,
            max_records=10,
            max_total_bytes=10_000,
            batch_size=10,
        ),
        record_timestamps={"demo-1": "2026-01-01T00:00:00+00:00"},
    )
    _write_raw_state(
        tmp_path,
        collection="demo_records",
        record_id="demo-1",
        version=3,
        revision=2,
        payload={"display_name": "changed", "enabled": True},
    )
    with pytest.raises(RepoForgeError) as stale:
        manager.apply_cleanup(preview)
    assert stale.value.code is ErrorCode.STATE_STALE


def test_cleanup_apply_resumes_after_process_crash(tmp_path: Path) -> None:
    for index in range(1, 3):
        _write_raw_state(
            tmp_path,
            collection="demo_records",
            record_id=f"demo-{index}",
            version=3,
            revision=1,
            payload={"display_name": f"demo-{index}", "enabled": True},
        )

    def crash_after_first_move(phase: str, record_id: str, index: int) -> None:
        if phase == "before_cleanup_move" and index == 1:
            raise SystemExit("simulated cleanup crash")

    manager = JsonStateLifecycleManager(
        tmp_path,
        InMemoryLockManager(),
        fault_injector=crash_after_first_move,
    )
    preview = manager.preview_cleanup(
        collection="demo_records",
        policy=StateRetentionPolicy(
            now="2026-07-01T00:00:00+00:00",
            retention_seconds=1,
            max_records=10,
            max_total_bytes=10_000,
            batch_size=10,
        ),
        record_timestamps={
            "demo-1": "2026-01-01T00:00:00+00:00",
            "demo-2": "2026-01-02T00:00:00+00:00",
        },
    )
    with pytest.raises(SystemExit):
        manager.apply_cleanup(preview)
    restarted = JsonStateLifecycleManager(tmp_path, InMemoryLockManager())
    report = restarted.apply_cleanup(preview)
    assert report.deleted == 2
    assert not list((tmp_path / "demo_records").glob("*.json"))


def test_integrity_scan_reports_schema_reference_corruption_and_quota_without_payloads(
    tmp_path: Path,
) -> None:
    _write_raw_state(
        tmp_path,
        collection="demo_records",
        record_id="demo-1",
        version=3,
        revision=1,
        payload={"display_name": "safe", "enabled": True},
    )
    _write_raw_state(
        tmp_path,
        collection="demo_records",
        record_id="demo-2",
        version=99,
        revision=1,
        payload={"secret": "must-not-appear"},
    )
    (tmp_path / "demo_records" / "demo-3.json").write_text("{bad", encoding="utf-8")
    manager = JsonStateRecoveryManager(tmp_path, InMemoryLockManager())
    report = manager.inspect_integrity(
        collection="demo_records",
        supported_versions=(SchemaVersion(3),),
        references=(StateRecordReference("demo-1", "missing-record", "receipt"),),
        max_records=10,
        max_total_bytes=100,
        max_findings=10,
    )
    assert report.healthy is False
    assert {item.code for item in report.findings} >= {
        "BYTE_QUOTA_EXCEEDED",
        "CORRUPT_RECORD",
        "MISSING_REFERENCE",
        "UNSUPPORTED_SCHEMA",
    }
    assert any(item.severity is IntegritySeverity.ERROR for item in report.findings)
    rendered = repr(report)
    assert "must-not-appear" not in rendered
    assert "{bad" not in rendered


def test_backup_preview_apply_is_deterministic_private_and_idempotent(tmp_path: Path) -> None:
    for index in range(1, 3):
        _write_raw_state(
            tmp_path,
            collection="demo_records",
            record_id=f"demo-{index}",
            version=3,
            revision=index,
            payload={"display_name": f"demo-{index}", "enabled": True},
        )
    manager = JsonStateRecoveryManager(tmp_path, InMemoryLockManager())
    preview = manager.preview_backup(
        collection="demo_records",
        destination_id="backup-destination",
        max_records=10,
        max_total_bytes=10_000,
    )
    assert len(preview.records) == 2
    assert len(preview.manifest_checksum) == 64
    assert (
        manager.preview_backup(
            collection="demo_records",
            destination_id="backup-destination",
            max_records=10,
            max_total_bytes=10_000,
        )
        == preview
    )

    destination = tmp_path.parent / "backup-destination"
    report = manager.apply_backup(preview, destination_root=destination)
    assert report.copied_records == 2
    assert report.total_bytes == preview.total_bytes
    assert (destination / "manifest.json").stat().st_mode & 0o777 == 0o600
    assert manager.apply_backup(preview, destination_root=destination) == report

    with pytest.raises(RepoForgeError) as invalid_destination:
        manager.preview_backup(
            collection="demo_records",
            destination_id="../unsafe",
            max_records=10,
            max_total_bytes=10_000,
        )
    assert invalid_destination.value.code is ErrorCode.STATE_INVALID


def test_restore_preview_detects_conflicts_and_overwrite_restores_with_backup(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    _write_raw_state(
        source,
        collection="demo_records",
        record_id="demo-1",
        version=3,
        revision=2,
        payload={"display_name": "from-backup", "enabled": True},
    )
    source_manager = JsonStateRecoveryManager(source, InMemoryLockManager())
    backup_preview = source_manager.preview_backup(
        collection="demo_records",
        destination_id="portable-backup",
    )
    backup_root = tmp_path / "portable-backup"
    source_manager.apply_backup(backup_preview, destination_root=backup_root)

    original_destination = _write_raw_state(
        destination,
        collection="demo_records",
        record_id="demo-1",
        version=3,
        revision=1,
        payload={"display_name": "local", "enabled": True},
    )
    destination_manager = JsonStateRecoveryManager(destination, InMemoryLockManager())
    conflict = destination_manager.preview_restore(
        backup_root=backup_root,
        destination_id="destination-state",
        overwrite=False,
        max_records=10,
        max_total_bytes=10_000,
    )
    assert conflict.conflicts == (("demo-1", "different_existing_record"),)
    with pytest.raises(RepoForgeError) as blocked:
        destination_manager.apply_restore(conflict, backup_root=backup_root)
    assert blocked.value.code is ErrorCode.ALREADY_EXISTS

    preview = destination_manager.preview_restore(
        backup_root=backup_root,
        destination_id="destination-state",
        overwrite=True,
        max_records=10,
        max_total_bytes=10_000,
    )
    report = destination_manager.apply_restore(preview, backup_root=backup_root)
    assert report.restored_records == 1
    assert report.replaced_records == 1
    assert _read_raw_state(destination, "demo_records", "demo-1")["payload"] == {
        "display_name": "from-backup",
        "enabled": True,
    }
    destination_backup = (
        destination
        / ".state-lifecycle"
        / "backups"
        / preview.restore_id
        / "destination"
        / "demo-1.json"
    )
    assert destination_backup.read_bytes() == original_destination
    assert destination_manager.apply_restore(preview, backup_root=backup_root) == report


def test_restore_rejects_corrupt_backup_and_rolls_back_failure(tmp_path: Path) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    for index in range(1, 3):
        _write_raw_state(
            source,
            collection="demo_records",
            record_id=f"demo-{index}",
            version=3,
            revision=1,
            payload={"display_name": f"source-{index}", "enabled": True},
        )
    source_manager = JsonStateRecoveryManager(source, InMemoryLockManager())
    backup = source_manager.preview_backup(
        collection="demo_records",
        destination_id="portable-backup",
    )
    backup_root = tmp_path / "portable-backup"
    source_manager.apply_backup(backup, destination_root=backup_root)
    (backup_root / "records" / "demo-2.json").write_text("tampered", encoding="utf-8")
    destination_manager = JsonStateRecoveryManager(destination, InMemoryLockManager())
    with pytest.raises(RepoForgeError) as corrupt:
        destination_manager.preview_restore(
            backup_root=backup_root,
            destination_id="destination-state",
            overwrite=False,
        )
    assert corrupt.value.code is ErrorCode.STATE_INVALID

    source_manager.apply_backup(backup, destination_root=backup_root, repair=True)

    def fail_second_restore(phase: str, record_id: str, index: int) -> None:
        if phase == "before_restore_write" and index == 1:
            raise OSError("injected restore failure")

    failing_manager = JsonStateRecoveryManager(
        destination,
        InMemoryLockManager(),
        fault_injector=fail_second_restore,
    )
    preview = failing_manager.preview_restore(
        backup_root=backup_root,
        destination_id="destination-state",
        overwrite=False,
    )
    with pytest.raises(RepoForgeError) as failed:
        failing_manager.apply_restore(preview, backup_root=backup_root)
    assert failed.value.code is ErrorCode.STATE_PERSISTENCE_FAILED
    assert not list((destination / "demo_records").glob("*.json"))
