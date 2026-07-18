"""Private atomic JSON stores for immutable execution plans and acceptances."""

from __future__ import annotations

import re
from dataclasses import asdict
from pathlib import Path

from ...domain.durable_state import SchemaVersion, StateCodec, StateEnvelope, StatePage
from ...domain.errors import ErrorCode, RepoForgeError
from ...domain.execution_plan import (
    EXECUTION_PLAN_SCHEMA_VERSION,
    ExecutionPlan,
    ExecutionPlanAcceptance,
    execution_plan_from_payload,
    new_plan_acceptance,
    plan_payload,
    validate_execution_plan,
)
from ...ports.execution_plan_store import ExecutionPlanAcceptanceStore, ExecutionPlanStore
from ...ports.locking import LockManager
from .json_state_repository import JsonStateRepository

_PLAN_ID = re.compile(r"^plan-[0-9a-f]{24}$")
_ACCEPTANCE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def _plan_id(value: str) -> str:
    if _PLAN_ID.fullmatch(value) is None:
        raise ValueError("invalid execution plan id")
    return value


def _acceptance_id(value: str) -> str:
    if _ACCEPTANCE_ID.fullmatch(value) is None:
        raise ValueError("invalid execution plan acceptance id")
    return value


class _PlanCodec(StateCodec[ExecutionPlan]):
    schema_version = SchemaVersion(EXECUTION_PLAN_SCHEMA_VERSION)

    def encode(self, value: ExecutionPlan) -> dict[str, object]:
        validate_execution_plan(value)
        return plan_payload(value)

    def decode(self, payload: dict[str, object]) -> ExecutionPlan:
        return execution_plan_from_payload(dict(payload))


class _AcceptanceCodec(StateCodec[ExecutionPlanAcceptance]):
    schema_version = SchemaVersion(EXECUTION_PLAN_SCHEMA_VERSION)

    def encode(self, value: ExecutionPlanAcceptance) -> dict[str, object]:
        return asdict(value)

    def decode(self, payload: dict[str, object]) -> ExecutionPlanAcceptance:
        required = {
            "acceptance_id",
            "plan_id",
            "plan_hash",
            "workspace_id",
            "task_id",
            "accepted_at",
            "schema_version",
        }
        if set(payload) != required:
            raise ValueError("execution plan acceptance fields are invalid")
        schema_version = payload["schema_version"]
        if not isinstance(schema_version, int) or isinstance(schema_version, bool):
            raise ValueError("execution plan acceptance schema is invalid")
        value = ExecutionPlanAcceptance(
            acceptance_id=str(payload["acceptance_id"]),
            plan_id=str(payload["plan_id"]),
            plan_hash=str(payload["plan_hash"]),
            workspace_id=str(payload["workspace_id"]),
            task_id=(str(payload["task_id"]) if payload["task_id"] is not None else None),
            accepted_at=str(payload["accepted_at"]),
            schema_version=schema_version,
        )
        if value.schema_version != EXECUTION_PLAN_SCHEMA_VERSION:
            raise ValueError("unsupported execution plan acceptance schema")
        _acceptance_id(value.acceptance_id)
        _plan_id(value.plan_id)
        if len(value.plan_hash) != 64:
            raise ValueError("execution plan acceptance hash is invalid")
        return value


class JsonExecutionPlanStore(ExecutionPlanStore):
    def __init__(self, state_root: Path, locks: LockManager) -> None:
        self._records = JsonStateRepository(
            state_root,
            collection="execution-plans",
            locks=locks,
            codec=_PlanCodec(),
            id_validator=_plan_id,
            max_record_bytes=1_000_000,
        )
        self.root = self._records.root

    def create(self, plan: ExecutionPlan) -> StateEnvelope[ExecutionPlan]:
        # Detect immutable identity collisions before validating the incoming payload.
        # A conflicting object may necessarily fail its content-addressed hash check,
        # but the durable-store contract still reports the existing identity binding.
        existing = self._records.read(plan.plan_id)
        if existing is not None:
            if existing.value == plan:
                return existing
            raise RepoForgeError(
                "Execution plan id is already bound to different content",
                code=ErrorCode.ALREADY_EXISTS,
            )
        validate_execution_plan(plan)
        return self._records.create(plan.plan_id, plan)

    def read(self, plan_id: str) -> StateEnvelope[ExecutionPlan] | None:
        return self._records.read(plan_id)

    def list_records(self, *, max_records: int = 200) -> StatePage[ExecutionPlan]:
        return self._records.list_records(max_records=max_records)


class JsonExecutionPlanAcceptanceStore(ExecutionPlanAcceptanceStore):
    def __init__(self, state_root: Path, locks: LockManager) -> None:
        self._records = JsonStateRepository(
            state_root,
            collection="execution-plan-acceptances",
            locks=locks,
            codec=_AcceptanceCodec(),
            id_validator=_acceptance_id,
            max_record_bytes=128_000,
        )
        self.root = self._records.root

    def accept(
        self,
        plan: ExecutionPlan,
        *,
        acceptance_id: str,
        task_id: str | None,
        accepted_at: str,
    ) -> StateEnvelope[ExecutionPlanAcceptance]:
        acceptance = new_plan_acceptance(
            plan,
            acceptance_id=acceptance_id,
            task_id=task_id,
            accepted_at=accepted_at,
        )
        existing = self._records.read(acceptance_id)
        if existing is not None:
            if existing.value == acceptance:
                return existing
            raise RepoForgeError(
                "Execution plan acceptance id is already bound to different content",
                code=ErrorCode.ALREADY_EXISTS,
            )
        for candidate in self._records.list_records(max_records=2_000).records:
            if candidate.value.plan_id != plan.plan_id:
                continue
            if candidate.value.plan_hash != plan.plan_hash or candidate.value.task_id != task_id:
                raise RepoForgeError(
                    "Execution plan already has a conflicting acceptance",
                    code=ErrorCode.ALREADY_EXISTS,
                )
            return candidate
        return self._records.create(acceptance_id, acceptance)

    def read(self, acceptance_id: str) -> StateEnvelope[ExecutionPlanAcceptance] | None:
        return self._records.read(acceptance_id)

    def read_for_plan(self, plan_id: str) -> StateEnvelope[ExecutionPlanAcceptance] | None:
        selected: StateEnvelope[ExecutionPlanAcceptance] | None = None
        for candidate in self._records.list_records(max_records=2_000).records:
            if candidate.value.plan_id == plan_id and (
                selected is None or candidate.value.accepted_at > selected.value.accepted_at
            ):
                selected = candidate
        return selected
