"""Persistence boundaries for immutable execution plans and acceptances."""

from __future__ import annotations

from typing import Protocol

from ..domain.durable_state import StateEnvelope, StatePage
from ..domain.execution_plan import ExecutionPlan, ExecutionPlanAcceptance


class ExecutionPlanStore(Protocol):
    def create(self, plan: ExecutionPlan) -> StateEnvelope[ExecutionPlan]: ...

    def read(self, plan_id: str) -> StateEnvelope[ExecutionPlan] | None: ...

    def list_records(self, *, max_records: int = 200) -> StatePage[ExecutionPlan]: ...


class ExecutionPlanAcceptanceStore(Protocol):
    def accept(
        self,
        plan: ExecutionPlan,
        *,
        acceptance_id: str,
        task_id: str | None,
        accepted_at: str,
    ) -> StateEnvelope[ExecutionPlanAcceptance]: ...

    def read(self, acceptance_id: str) -> StateEnvelope[ExecutionPlanAcceptance] | None: ...

    def read_for_plan(self, plan_id: str) -> StateEnvelope[ExecutionPlanAcceptance] | None: ...
