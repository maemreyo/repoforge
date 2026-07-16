"""Durable TaskCapsule persistence boundary."""

from __future__ import annotations

from typing import Protocol

from ..domain.durable_state import Revision, StateEnvelope, StatePage
from ..domain.task_capsule import TaskCapsule


class TaskStore(Protocol):
    def create(self, task: TaskCapsule) -> StateEnvelope[TaskCapsule]: ...

    def read(self, task_id: str) -> StateEnvelope[TaskCapsule] | None: ...

    def save(
        self, task: TaskCapsule, *, expected_revision: Revision
    ) -> StateEnvelope[TaskCapsule]: ...

    def list_records(self, *, max_records: int) -> StatePage[TaskCapsule]: ...

    def delete(self, task_id: str) -> None: ...
