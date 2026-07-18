"""Persistence-layer coverage for TaskCapsule v2 (#208): round-trip, CAS soak, and the v1->v2
migration through the existing durable-state lifecycle machinery (no one-way migration)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from repoforge.adapters.persistence.json_state_lifecycle import JsonStateLifecycleManager
from repoforge.adapters.persistence.json_task_store import (
    TASK_CAPSULE_MIGRATION_STEPS,
    TASK_CAPSULES_COLLECTION,
    JsonTaskStore,
)
from repoforge.domain.durable_state import Revision, SchemaVersion
from repoforge.domain.errors import RepoForgeError
from repoforge.domain.rules_engine import OverridePolicy
from repoforge.domain.state_lifecycle import StateMigrationRegistry
from repoforge.domain.task_capsule import (
    InstructionOrigin,
    RecordedBy,
    TaskCapsule,
    TrustLevel,
    acquire_lease,
    add_instruction,
    add_override,
    escalate_rule,
    record_guide_delivered,
    record_mutation,
)
from repoforge.testing.fakes import InMemoryLockManager


def _task() -> TaskCapsule:
    return TaskCapsule.new(
        task_id="task-" + "b" * 24,
        intent="intent",
        acceptance_criteria=("done",),
        constraints=(),
        repo_ids=("demo",),
        created_at="2026-07-18T00:00:00+00:00",
        path_scope=("src/**",),
    )


def test_v2_capsule_round_trips_through_the_store_with_all_new_fields_populated(
    tmp_path: Path,
) -> None:
    store = JsonTaskStore(tmp_path, InMemoryLockManager())
    task = _task()
    task = add_instruction(
        task,
        instruction_id="instr-1",
        content="content",
        asserted_origin=InstructionOrigin.ISSUE,
        recorded_by=RecordedBy.SYSTEM,
        trust=TrustLevel.VERIFIED,
        updated_at="2026-07-18T00:01:00+00:00",
    )
    task = add_override(
        task,
        override_id="ov-1",
        rule_id="rule.x",
        override_policy=OverridePolicy.APPROVAL,
        scope=("src/**",),
        reason="reason",
        actor="operator",
        updated_at="2026-07-18T00:02:00+00:00",
    )
    task = record_guide_delivered(task, "guide-1", updated_at="2026-07-18T00:03:00+00:00")
    task = escalate_rule(task, "rule.y", updated_at="2026-07-18T00:04:00+00:00")
    task = record_mutation(task, updated_at="2026-07-18T00:05:00+00:00")
    task = acquire_lease(
        task,
        holder="foreman-1",
        expires_at="2026-07-18T01:00:00+00:00",
        updated_at="2026-07-18T00:06:00+00:00",
    )

    store.create(task)
    loaded = store.read(task.task_id)
    assert loaded is not None
    assert loaded.value == task
    assert loaded.schema_version.value == 2


def test_cas_rejects_stale_writer_under_a_soak_of_concurrent_updates(tmp_path: Path) -> None:
    store = JsonTaskStore(tmp_path, InMemoryLockManager())
    task = _task()
    envelope = store.create(task)

    successes = 0
    stale_rejections = 0
    current_revision = envelope.revision
    current_task = task
    for i in range(100):
        candidate = record_mutation(current_task, updated_at=f"2026-07-18T00:{i:02d}:00+00:00")
        try:
            saved = store.save(candidate, expected_revision=current_revision)
        except RepoForgeError:
            stale_rejections += 1
            continue
        successes += 1
        current_revision = saved.revision
        current_task = candidate

        # A second writer racing with a now-stale revision must be rejected, never silently
        # accepted (proves CAS holds under repeated contention, not just once).
        with pytest.raises(RepoForgeError):
            store.save(
                record_mutation(task, updated_at="2026-07-18T99:00:00+00:00"),
                expected_revision=Revision(1),
            )

    assert successes == 100
    final = store.read(task.task_id)
    assert final is not None
    assert final.value.mutation_count == 100


def test_v1_capsule_migrates_to_v2_with_backup_and_reversal(tmp_path: Path) -> None:
    # A v1-shaped record predating #208, written directly (no schema_version bump helper
    # exists yet in production code -- this simulates what's actually on disk from before).
    v1_payload = {
        "acceptance_criteria": [
            {
                "criterion_id": "criterion-1",
                "summary": "done",
                "status": "pending",
                "evidence_ids": [],
            }
        ],
        "accepted_plan_id": None,
        "active_config_generation": None,
        "blocked_reason": None,
        "constraints": [],
        "created_at": "2026-07-18T00:00:00+00:00",
        "current_phase": "intake",
        "decisions": [],
        "evidence_snapshot_ids": [],
        "intent": "intent",
        "next_safe_actions": [],
        "open_questions": [],
        "receipt_ids": [],
        "repo_ids": ["demo"],
        "source_issue_or_pr": None,
        "state": "draft",
        "task_id": "task-" + "c" * 24,
        "updated_at": "2026-07-18T00:00:00+00:00",
        "workspace_bindings": [],
    }
    collection_dir = tmp_path / TASK_CAPSULES_COLLECTION
    collection_dir.mkdir(parents=True)
    record_path = collection_dir / f"{v1_payload['task_id']}.json"
    record_path.write_text(
        json.dumps(
            {
                "record_id": v1_payload["task_id"],
                "schema_version": 1,
                "revision": 1,
                "payload": v1_payload,
            }
        ),
        encoding="utf-8",
    )

    manager = JsonStateLifecycleManager(tmp_path, InMemoryLockManager())
    registry = StateMigrationRegistry(TASK_CAPSULE_MIGRATION_STEPS)

    preview = manager.preview_migration(
        collection=TASK_CAPSULES_COLLECTION,
        registry=registry,
        target_version=SchemaVersion(2),
        max_records=10,
    )
    assert preview.migrated_records == 1
    report = manager.apply_migration(preview, registry=registry)
    assert report.migrated == 1
    assert report.rolled_back is False

    store = JsonTaskStore(tmp_path, InMemoryLockManager())
    loaded = store.read(v1_payload["task_id"])
    assert loaded is not None
    assert loaded.schema_version.value == 2
    assert loaded.value.principal  # default applied
    assert loaded.value.instructions == ()
    assert loaded.value.task_revision == 1

    # No one-way migration: reverse plan brings it back to a v1-shaped payload.
    reverse_preview = manager.preview_migration(
        collection=TASK_CAPSULES_COLLECTION,
        registry=registry,
        target_version=SchemaVersion(1),
        max_records=10,
    )
    reverse_report = manager.apply_migration(reverse_preview, registry=registry)
    assert reverse_report.migrated == 1
    raw_after_reverse = json.loads(record_path.read_text(encoding="utf-8"))
    assert raw_after_reverse["schema_version"] == 1
    assert "principal" not in raw_after_reverse["payload"]
