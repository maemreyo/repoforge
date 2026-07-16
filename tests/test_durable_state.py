from __future__ import annotations

import importlib
import json
import os
from dataclasses import dataclass
from pathlib import Path

import pytest

from repoforge.adapters.persistence.json_state_repository import JsonStateRepository
from repoforge.domain.durable_state import Revision, SchemaVersion, StateEnvelope
from repoforge.domain.errors import ErrorCode, RepoForgeError
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
