"""Immutable, snapshot-bound execution plans and acceptance records."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, NoReturn

from .errors import ErrorCode, RepoForgeError

EXECUTION_PLAN_SCHEMA_VERSION = 1
_PLAN_ID = re.compile(r"^plan-[0-9a-f]{24}$")
_STAGE_ID = re.compile(r"^[a-z][a-z0-9._-]{0,63}$")
_SAFE_TARGET = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SHA40 = re.compile(r"^[0-9a-f]{40}$")
_SHA64 = re.compile(r"^[0-9a-f]{64}$")


class PlanStageKind(str, Enum):
    DIAGNOSTIC = "diagnostic"
    PROFILE = "profile"


class PlanStageBoundary(str, Enum):
    ITERATION = "iteration"
    FINAL = "final"


class PlanStageMutability(str, Enum):
    READ_ONLY = "read_only"
    WORKSPACE_WRITE = "workspace_write"


class StageFailurePolicy(str, Enum):
    REQUIRED = "required"
    OPTIONAL = "optional"


@dataclass(frozen=True, slots=True)
class PlanStage:
    stage_id: str
    kind: PlanStageKind
    target: str
    selector: str | None
    dependencies: tuple[str, ...]
    boundary: PlanStageBoundary
    working_directory: str | None
    timeout_seconds: int
    mutability: PlanStageMutability
    network_policy: str
    failure_policy: StageFailurePolicy
    artifact_paths: tuple[str, ...]

    def __post_init__(self) -> None:
        if _STAGE_ID.fullmatch(self.stage_id) is None:
            _invalid("Plan stage id is invalid")
        if _SAFE_TARGET.fullmatch(self.target) is None:
            _invalid("Plan stage target is invalid")
        if self.selector is not None and (
            not self.selector or len(self.selector) > 256 or any(ord(c) < 32 for c in self.selector)
        ):
            _invalid("Plan stage selector is invalid")
        if len(self.dependencies) > 32 or len(set(self.dependencies)) != len(self.dependencies):
            _invalid("Plan stage dependencies must be unique and bounded")
        if not 1 <= self.timeout_seconds <= 86_400:
            _invalid("Plan stage timeout must be between 1 and 86400 seconds")
        if self.network_policy not in {"none", "local_only", "restricted", "external"}:
            _invalid("Plan stage network policy is unsupported")
        if len(self.artifact_paths) > 32:
            _invalid("Plan stage artifact paths exceed the reviewed bound")
        for path in self.artifact_paths:
            if not path or len(path) > 256 or path.startswith("/") or ".." in path.split("/"):
                _invalid("Plan stage artifact path is unsafe")
        if self.boundary is PlanStageBoundary.FINAL:
            if self.kind is not PlanStageKind.PROFILE:
                _invalid("The final plan stage must be a profile")
            if self.failure_policy is not StageFailurePolicy.REQUIRED:
                _invalid("The final plan stage must be required")

    def definition_payload(self) -> dict[str, object]:
        return {
            "artifact_paths": list(self.artifact_paths),
            "boundary": self.boundary.value,
            "dependencies": list(self.dependencies),
            "failure_policy": self.failure_policy.value,
            "kind": self.kind.value,
            "mutability": self.mutability.value,
            "network_policy": self.network_policy,
            "selector": self.selector,
            "stage_id": self.stage_id,
            "target": self.target,
            "timeout_seconds": self.timeout_seconds,
            "working_directory": self.working_directory,
        }

    @property
    def definition_hash(self) -> str:
        return _digest(self.definition_payload())


@dataclass(frozen=True, slots=True)
class ExecutionPlanBinding:
    head_sha: str
    workspace_fingerprint: str
    config_generation: str
    policy_hash: str
    assessment_snapshot_id: str
    evidence_snapshot_ids: tuple[str, ...]
    risk_assessment_hash: str
    recommendation_hash: str

    def __post_init__(self) -> None:
        if _SHA40.fullmatch(self.head_sha) is None:
            _invalid("Execution plan HEAD SHA is invalid")
        for name, value in (
            ("workspace fingerprint", self.workspace_fingerprint),
            ("configuration generation", self.config_generation),
            ("policy hash", self.policy_hash),
            ("assessment snapshot id", self.assessment_snapshot_id),
            ("risk assessment hash", self.risk_assessment_hash),
            ("recommendation hash", self.recommendation_hash),
        ):
            if _SHA64.fullmatch(value) is None:
                _invalid(f"Execution plan {name} is invalid")
        if len(self.evidence_snapshot_ids) > 64:
            _invalid("Execution plan evidence snapshot list is too large")
        if len(set(self.evidence_snapshot_ids)) != len(self.evidence_snapshot_ids):
            _invalid("Execution plan evidence snapshot ids must be unique")
        if any(_SHA64.fullmatch(value) is None for value in self.evidence_snapshot_ids):
            _invalid("Execution plan evidence snapshot id is invalid")

    def payload(self) -> dict[str, object]:
        return {
            "assessment_snapshot_id": self.assessment_snapshot_id,
            "config_generation": self.config_generation,
            "evidence_snapshot_ids": list(self.evidence_snapshot_ids),
            "head_sha": self.head_sha,
            "policy_hash": self.policy_hash,
            "recommendation_hash": self.recommendation_hash,
            "risk_assessment_hash": self.risk_assessment_hash,
            "workspace_fingerprint": self.workspace_fingerprint,
        }


@dataclass(frozen=True, slots=True)
class ExecutionPlan:
    plan_id: str
    plan_hash: str
    task_id: str | None
    workspace_id: str
    binding: ExecutionPlanBinding
    ordered_stages: tuple[PlanStage, ...]
    final_profile: str
    stage_definition_hash: str
    created_at: str
    expires_at: str | None
    schema_version: int = EXECUTION_PLAN_SCHEMA_VERSION


@dataclass(frozen=True, slots=True)
class ExecutionPlanState:
    head_sha: str
    workspace_fingerprint: str
    config_generation: str
    policy_hash: str
    risk_assessment_hash: str
    recommendation_hash: str
    stage_definition_hash: str
    now: str


@dataclass(frozen=True, slots=True)
class ExecutionPlanAcceptance:
    acceptance_id: str
    plan_id: str
    plan_hash: str
    workspace_id: str
    task_id: str | None
    accepted_at: str
    schema_version: int = EXECUTION_PLAN_SCHEMA_VERSION


def _invalid(message: str) -> NoReturn:
    raise RepoForgeError(
        message,
        code=ErrorCode.STATE_INVALID,
        safe_next_action="Create a fresh plan from a current workspace assessment.",
    )


def _digest(payload: object) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _iso(value: str, field: str) -> datetime:
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise RepoForgeError(
            f"Execution plan {field} is not an ISO timestamp",
            code=ErrorCode.STATE_INVALID,
        ) from exc


def stage_definitions_hash(stages: tuple[PlanStage, ...]) -> str:
    return _digest([stage.definition_payload() for stage in stages])


def _validate_graph(stages: tuple[PlanStage, ...], final_profile: str) -> None:
    if not stages or len(stages) > 128:
        _invalid("Execution plan must contain between 1 and 128 stages")
    ids = [stage.stage_id for stage in stages]
    if len(ids) != len(set(ids)):
        _invalid("Execution plan stage ids must be unique")
    known = set(ids)
    order = {stage_id: index for index, stage_id in enumerate(ids)}
    for stage in stages:
        unknown = sorted(set(stage.dependencies) - known)
        if unknown:
            _invalid(f"Execution plan stage {stage.stage_id!r} has unknown dependencies: {unknown}")
        if stage.stage_id in stage.dependencies:
            _invalid("Execution plan stage cannot depend on itself")
        if any(order[dependency] >= order[stage.stage_id] for dependency in stage.dependencies):
            _invalid("Execution plan stages must be supplied in dependency order")
    visiting: set[str] = set()
    visited: set[str] = set()
    by_id = {stage.stage_id: stage for stage in stages}

    def visit(stage_id: str) -> None:
        if stage_id in visiting:
            _invalid("Execution plan dependency graph contains a cycle")
        if stage_id in visited:
            return
        visiting.add(stage_id)
        for dependency in by_id[stage_id].dependencies:
            visit(dependency)
        visiting.remove(stage_id)
        visited.add(stage_id)

    for stage_id in ids:
        visit(stage_id)
    finals = [stage for stage in stages if stage.boundary is PlanStageBoundary.FINAL]
    if len(finals) != 1:
        _invalid("Execution plan requires exactly one final stage")
    final = finals[0]
    if final is not stages[-1] or final.target != final_profile:
        _invalid("Execution plan final stage must be last and target final_profile")


def _semantic_payload(
    *,
    task_id: str | None,
    workspace_id: str,
    binding: ExecutionPlanBinding,
    ordered_stages: tuple[PlanStage, ...],
    final_profile: str,
    stage_definition_hash: str,
    created_at: str,
    expires_at: str | None,
    schema_version: int,
) -> dict[str, object]:
    return {
        "binding": binding.payload(),
        "created_at": created_at,
        "expires_at": expires_at,
        "final_profile": final_profile,
        "ordered_stages": [stage.definition_payload() for stage in ordered_stages],
        "schema_version": schema_version,
        "stage_definition_hash": stage_definition_hash,
        "task_id": task_id,
        "workspace_id": workspace_id,
    }


def create_execution_plan(
    *,
    task_id: str | None,
    workspace_id: str,
    binding: ExecutionPlanBinding,
    ordered_stages: tuple[PlanStage, ...],
    final_profile: str,
    created_at: str,
    expires_at: str | None = None,
) -> ExecutionPlan:
    if not workspace_id or len(workspace_id) > 128:
        _invalid("Execution plan workspace id is invalid")
    if task_id is not None and (not task_id or len(task_id) > 128):
        _invalid("Execution plan task id is invalid")
    if _SAFE_TARGET.fullmatch(final_profile) is None:
        _invalid("Execution plan final profile is invalid")
    created = _iso(created_at, "created_at")
    if expires_at is not None and _iso(expires_at, "expires_at") <= created:
        _invalid("Execution plan expiry must be after creation")
    _validate_graph(ordered_stages, final_profile)
    definition_hash = stage_definitions_hash(ordered_stages)
    semantic = _semantic_payload(
        task_id=task_id,
        workspace_id=workspace_id,
        binding=binding,
        ordered_stages=ordered_stages,
        final_profile=final_profile,
        stage_definition_hash=definition_hash,
        created_at=created_at,
        expires_at=expires_at,
        schema_version=EXECUTION_PLAN_SCHEMA_VERSION,
    )
    plan_hash = _digest(semantic)
    return ExecutionPlan(
        plan_id=f"plan-{plan_hash[:24]}",
        plan_hash=plan_hash,
        task_id=task_id,
        workspace_id=workspace_id,
        binding=binding,
        ordered_stages=ordered_stages,
        final_profile=final_profile,
        stage_definition_hash=definition_hash,
        created_at=created_at,
        expires_at=expires_at,
    )


def plan_payload(plan: ExecutionPlan) -> dict[str, object]:
    return {
        **_semantic_payload(
            task_id=plan.task_id,
            workspace_id=plan.workspace_id,
            binding=plan.binding,
            ordered_stages=plan.ordered_stages,
            final_profile=plan.final_profile,
            stage_definition_hash=plan.stage_definition_hash,
            created_at=plan.created_at,
            expires_at=plan.expires_at,
            schema_version=plan.schema_version,
        ),
        "plan_hash": plan.plan_hash,
        "plan_id": plan.plan_id,
    }


def validate_execution_plan(plan: ExecutionPlan) -> ExecutionPlan:
    if plan.schema_version != EXECUTION_PLAN_SCHEMA_VERSION:
        _invalid("Execution plan schema version is unsupported")
    expected = create_execution_plan(
        task_id=plan.task_id,
        workspace_id=plan.workspace_id,
        binding=plan.binding,
        ordered_stages=plan.ordered_stages,
        final_profile=plan.final_profile,
        created_at=plan.created_at,
        expires_at=plan.expires_at,
    )
    if plan.plan_id != expected.plan_id or _PLAN_ID.fullmatch(plan.plan_id) is None:
        _invalid("Execution plan id does not match its content")
    if (
        plan.plan_hash != expected.plan_hash
        or plan.stage_definition_hash != expected.stage_definition_hash
    ):
        _invalid("Execution plan hashes do not match its content")
    return plan


def validate_plan_current(plan: ExecutionPlan, current: ExecutionPlanState) -> tuple[str, ...]:
    validate_execution_plan(plan)
    reasons: list[str] = []
    comparisons = (
        ("head_sha", plan.binding.head_sha, current.head_sha),
        (
            "workspace_fingerprint",
            plan.binding.workspace_fingerprint,
            current.workspace_fingerprint,
        ),
        ("config_generation", plan.binding.config_generation, current.config_generation),
        ("policy_hash", plan.binding.policy_hash, current.policy_hash),
        ("risk_assessment", plan.binding.risk_assessment_hash, current.risk_assessment_hash),
        ("recommendation", plan.binding.recommendation_hash, current.recommendation_hash),
        ("stage_definition", plan.stage_definition_hash, current.stage_definition_hash),
    )
    for name, expected, actual in comparisons:
        if expected != actual:
            reasons.append(name)
    if plan.expires_at is not None and _iso(current.now, "current time") > _iso(
        plan.expires_at, "expires_at"
    ):
        reasons.append("expired")
    return tuple(reasons)


def new_plan_acceptance(
    plan: ExecutionPlan,
    *,
    acceptance_id: str,
    task_id: str | None,
    accepted_at: str,
) -> ExecutionPlanAcceptance:
    validate_execution_plan(plan)
    if not acceptance_id or len(acceptance_id) > 128:
        _invalid("Execution plan acceptance id is invalid")
    _iso(accepted_at, "accepted_at")
    if task_id is not None and plan.task_id is not None and task_id != plan.task_id:
        raise RepoForgeError(
            "Execution plan acceptance task does not match the plan",
            code=ErrorCode.ALREADY_EXISTS,
        )
    return ExecutionPlanAcceptance(
        acceptance_id=acceptance_id,
        plan_id=plan.plan_id,
        plan_hash=plan.plan_hash,
        workspace_id=plan.workspace_id,
        task_id=task_id,
        accepted_at=accepted_at,
    )


def execution_plan_from_payload(payload: dict[str, Any]) -> ExecutionPlan:
    binding_raw = payload.get("binding")
    stages_raw = payload.get("ordered_stages")
    if not isinstance(binding_raw, dict) or not isinstance(stages_raw, list):
        _invalid("Execution plan payload is incomplete")
    binding = ExecutionPlanBinding(
        head_sha=str(binding_raw.get("head_sha", "")),
        workspace_fingerprint=str(binding_raw.get("workspace_fingerprint", "")),
        config_generation=str(binding_raw.get("config_generation", "")),
        policy_hash=str(binding_raw.get("policy_hash", "")),
        assessment_snapshot_id=str(binding_raw.get("assessment_snapshot_id", "")),
        evidence_snapshot_ids=tuple(
            str(item) for item in binding_raw.get("evidence_snapshot_ids", [])
        ),
        risk_assessment_hash=str(binding_raw.get("risk_assessment_hash", "")),
        recommendation_hash=str(binding_raw.get("recommendation_hash", "")),
    )
    stages: list[PlanStage] = []
    for raw in stages_raw:
        if not isinstance(raw, dict):
            _invalid("Execution plan stage payload must be an object")
        stages.append(
            PlanStage(
                stage_id=str(raw.get("stage_id", "")),
                kind=PlanStageKind(str(raw.get("kind", ""))),
                target=str(raw.get("target", "")),
                selector=(str(raw["selector"]) if raw.get("selector") is not None else None),
                dependencies=tuple(str(item) for item in raw.get("dependencies", [])),
                boundary=PlanStageBoundary(str(raw.get("boundary", ""))),
                working_directory=(
                    str(raw["working_directory"])
                    if raw.get("working_directory") is not None
                    else None
                ),
                timeout_seconds=int(raw.get("timeout_seconds", 0)),
                mutability=PlanStageMutability(str(raw.get("mutability", ""))),
                network_policy=str(raw.get("network_policy", "")),
                failure_policy=StageFailurePolicy(str(raw.get("failure_policy", ""))),
                artifact_paths=tuple(str(item) for item in raw.get("artifact_paths", [])),
            )
        )
    plan = ExecutionPlan(
        plan_id=str(payload.get("plan_id", "")),
        plan_hash=str(payload.get("plan_hash", "")),
        task_id=(str(payload["task_id"]) if payload.get("task_id") is not None else None),
        workspace_id=str(payload.get("workspace_id", "")),
        binding=binding,
        ordered_stages=tuple(stages),
        final_profile=str(payload.get("final_profile", "")),
        stage_definition_hash=str(payload.get("stage_definition_hash", "")),
        created_at=str(payload.get("created_at", "")),
        expires_at=(str(payload["expires_at"]) if payload.get("expires_at") is not None else None),
        schema_version=int(payload.get("schema_version", 0)),
    )
    return validate_execution_plan(plan)
