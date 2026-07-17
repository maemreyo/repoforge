"""Application orchestration for durable TaskCapsules."""

from __future__ import annotations

from ...domain.durable_state import Revision, StateEnvelope, StatePage
from ...domain.task_capsule import TaskCapsule, TaskState, transition_task
from ...ports.clock import Clock
from ...ports.ids import IdGenerator
from ...ports.task_store import TaskStore


class TaskCapsuleService:
    def __init__(self, *, store: TaskStore, clock: Clock, ids: IdGenerator) -> None:
        self._store = store
        self._clock = clock
        self._ids = ids

    def create(
        self,
        *,
        intent: str,
        acceptance_criteria: tuple[str, ...],
        constraints: tuple[str, ...] = (),
        repo_ids: tuple[str, ...] = (),
    ) -> StateEnvelope[TaskCapsule]:
        now = self._clock.now_iso()
        task = TaskCapsule.new(
            task_id=f"task-{self._ids.new_hex(24)}",
            intent=intent,
            acceptance_criteria=acceptance_criteria,
            constraints=constraints,
            repo_ids=repo_ids,
            created_at=now,
        )
        return self._store.create(task)

    def read(self, task_id: str) -> StateEnvelope[TaskCapsule] | None:
        return self._store.read(task_id)

    def resume(self, task_id: str) -> dict[str, object]:
        envelope = self._store.read(task_id)
        if envelope is None:
            raise ValueError(f"Unknown task: {task_id}")
        return {
            "revision": envelope.revision.value,
            **envelope.value.resume_projection(),
        }

    def transition(
        self,
        task_id: str,
        state: TaskState,
        *,
        expected_revision: Revision,
    ) -> StateEnvelope[TaskCapsule]:
        current = self._store.read(task_id)
        if current is None:
            raise ValueError(f"Unknown task: {task_id}")
        updated = transition_task(current.value, state, updated_at=self._clock.now_iso())
        return self._store.save(updated, expected_revision=expected_revision)

    def list_records(self, *, max_records: int = 100) -> StatePage[TaskCapsule]:
        return self._store.list_records(max_records=max_records)
