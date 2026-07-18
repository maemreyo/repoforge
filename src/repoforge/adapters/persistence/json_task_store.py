"""Private atomic TaskCapsule persistence over the shared durable-state substrate."""

from __future__ import annotations

from pathlib import Path

from ...domain.durable_state import Revision, SchemaVersion, StateEnvelope, StatePage
from ...domain.state_lifecycle import StateMigrationStep
from ...domain.task_capsule import (
    LOCAL_OPERATOR_PRINCIPAL,
    TASK_CAPSULE_SCHEMA_VERSION,
    CriterionStatus,
    InstructionOrigin,
    RecordedBy,
    TaskAction,
    TaskCapsule,
    TaskCriterion,
    TaskDecision,
    TaskInstruction,
    TaskOverride,
    TaskQuestion,
    TaskState,
    TrustLevel,
    WorkspaceBinding,
    validate_task_id,
)
from ...ports.locking import LockManager
from .json_state_repository import JsonStateRepository

TASK_CAPSULES_COLLECTION = "task-capsules"

#: v1 -> v2 (#208): additive fields only, each with a safe, semantically-neutral default so an
#: existing v1 record decodes cleanly after migration and round-trips back losslessly.
_V2_ADDED_FIELDS = {
    "principal": LOCAL_OPERATOR_PRINCIPAL,
    "path_scope": [],
    "instructions": [],
    "overrides": [],
    "task_revision": 1,
    "guides_delivered": [],
    "escalated_rules": [],
    "mutation_count": 0,
    "lease_holder": None,
    "lease_expires_at": None,
}


def _migrate_v1_to_v2(payload: dict[str, object]) -> dict[str, object]:
    return {**payload, **_V2_ADDED_FIELDS}


def _migrate_v2_to_v1(payload: dict[str, object]) -> dict[str, object]:
    return {key: value for key, value in payload.items() if key not in _V2_ADDED_FIELDS}


TASK_CAPSULE_MIGRATION_STEPS: tuple[StateMigrationStep, ...] = (
    StateMigrationStep(
        collection=TASK_CAPSULES_COLLECTION,
        from_version=SchemaVersion(1),
        to_version=SchemaVersion(2),
        forward=_migrate_v1_to_v2,
        reverse=_migrate_v2_to_v1,
    ),
)


def _required_fields(payload: dict[str, object], expected: set[str]) -> None:
    if set(payload) != expected:
        raise ValueError("task capsule payload fields do not match schema version 1")


def _object(value: object, *, name: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    return {str(key): item for key, item in value.items()}


def _objects(value: object, *, name: str) -> list[dict[str, object]]:
    if not isinstance(value, list):
        raise ValueError(f"{name} must be a list")
    return [_object(item, name=name) for item in value]


def _strings(value: object, *, name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{name} must be a string list")
    return tuple(value)


class TaskCapsuleCodec:
    schema_version = SchemaVersion(TASK_CAPSULE_SCHEMA_VERSION)

    def encode(self, value: TaskCapsule) -> dict[str, object]:
        return {
            "acceptance_criteria": [
                {
                    "criterion_id": item.criterion_id,
                    "summary": item.summary,
                    "status": item.status.value,
                    "evidence_ids": list(item.evidence_ids),
                }
                for item in value.acceptance_criteria
            ],
            "accepted_plan_id": value.accepted_plan_id,
            "active_config_generation": value.active_config_generation,
            "blocked_reason": value.blocked_reason,
            "constraints": list(value.constraints),
            "created_at": value.created_at,
            "current_phase": value.current_phase,
            "decisions": [
                {
                    "decision_id": item.decision_id,
                    "summary": item.summary,
                    "outcome": item.outcome,
                    "decided_at": item.decided_at,
                }
                for item in value.decisions
            ],
            "evidence_snapshot_ids": list(value.evidence_snapshot_ids),
            "intent": value.intent,
            "next_safe_actions": [
                {"action": item.action, "reason": item.reason, "required": item.required}
                for item in value.next_safe_actions
            ],
            "open_questions": [
                {"question_id": item.question_id, "summary": item.summary}
                for item in value.open_questions
            ],
            "receipt_ids": list(value.receipt_ids),
            "repo_ids": list(value.repo_ids),
            "source_issue_or_pr": value.source_issue_or_pr,
            "state": value.state.value,
            "task_id": value.task_id,
            "updated_at": value.updated_at,
            "workspace_bindings": [
                {
                    "workspace_id": item.workspace_id,
                    "repo_id": item.repo_id,
                    "head_sha": item.head_sha,
                    "workspace_fingerprint": item.workspace_fingerprint,
                    "stale": item.stale,
                }
                for item in value.workspace_bindings
            ],
            "principal": value.principal,
            "path_scope": list(value.path_scope),
            "task_revision": value.task_revision,
            "instructions": [item.as_dict() for item in value.instructions],
            "overrides": [item.as_dict() for item in value.overrides],
            "guides_delivered": list(value.guides_delivered),
            "escalated_rules": list(value.escalated_rules),
            "mutation_count": value.mutation_count,
            "lease_holder": value.lease_holder,
            "lease_expires_at": value.lease_expires_at,
        }

    def decode(self, payload: dict[str, object]) -> TaskCapsule:
        _required_fields(
            payload,
            {
                "acceptance_criteria",
                "accepted_plan_id",
                "active_config_generation",
                "blocked_reason",
                "constraints",
                "created_at",
                "current_phase",
                "decisions",
                "evidence_snapshot_ids",
                "intent",
                "next_safe_actions",
                "open_questions",
                "receipt_ids",
                "repo_ids",
                "source_issue_or_pr",
                "state",
                "task_id",
                "updated_at",
                "workspace_bindings",
                "principal",
                "path_scope",
                "task_revision",
                "instructions",
                "overrides",
                "guides_delivered",
                "escalated_rules",
                "mutation_count",
                "lease_holder",
                "lease_expires_at",
            },
        )
        criteria: list[TaskCriterion] = []
        for raw in _objects(payload["acceptance_criteria"], name="acceptance_criteria"):
            _required_fields(raw, {"criterion_id", "summary", "status", "evidence_ids"})
            criteria.append(
                TaskCriterion(
                    str(raw["criterion_id"]),
                    str(raw["summary"]),
                    CriterionStatus(str(raw["status"])),
                    _strings(raw["evidence_ids"], name="criterion evidence_ids"),
                )
            )
        decisions: list[TaskDecision] = []
        for raw in _objects(payload["decisions"], name="decisions"):
            _required_fields(raw, {"decision_id", "summary", "outcome", "decided_at"})
            decisions.append(
                TaskDecision(
                    str(raw["decision_id"]),
                    str(raw["summary"]),
                    str(raw["outcome"]),
                    str(raw["decided_at"]),
                )
            )
        questions: list[TaskQuestion] = []
        for raw in _objects(payload["open_questions"], name="open_questions"):
            _required_fields(raw, {"question_id", "summary"})
            questions.append(TaskQuestion(str(raw["question_id"]), str(raw["summary"])))
        actions: list[TaskAction] = []
        for raw in _objects(payload["next_safe_actions"], name="next_safe_actions"):
            _required_fields(raw, {"action", "reason", "required"})
            required = raw["required"]
            if not isinstance(required, bool):
                raise ValueError("task action required must be boolean")
            actions.append(TaskAction(str(raw["action"]), str(raw["reason"]), required))
        bindings: list[WorkspaceBinding] = []
        for raw in _objects(payload["workspace_bindings"], name="workspace_bindings"):
            _required_fields(
                raw,
                {"workspace_id", "repo_id", "head_sha", "workspace_fingerprint", "stale"},
            )
            stale = raw["stale"]
            if not isinstance(stale, bool):
                raise ValueError("workspace binding stale must be boolean")
            bindings.append(
                WorkspaceBinding(
                    str(raw["workspace_id"]),
                    str(raw["repo_id"]),
                    str(raw["head_sha"]) if raw["head_sha"] is not None else None,
                    str(raw["workspace_fingerprint"])
                    if raw["workspace_fingerprint"] is not None
                    else None,
                    stale,
                )
            )
        generation = payload["active_config_generation"]
        if generation is not None and (
            not isinstance(generation, int) or isinstance(generation, bool)
        ):
            raise ValueError("active_config_generation must be an integer or null")

        instructions: list[TaskInstruction] = []
        for raw in _objects(payload["instructions"], name="instructions"):
            _required_fields(
                raw,
                {
                    "instruction_id",
                    "content",
                    "asserted_origin",
                    "recorded_by",
                    "trust",
                    "revision",
                    "scope",
                    "expiry",
                },
            )
            revision = raw["revision"]
            if not isinstance(revision, int) or isinstance(revision, bool):
                raise ValueError("instruction revision must be an integer")
            instructions.append(
                TaskInstruction(
                    instruction_id=str(raw["instruction_id"]),
                    content=str(raw["content"]),
                    asserted_origin=InstructionOrigin(str(raw["asserted_origin"])),
                    recorded_by=RecordedBy(str(raw["recorded_by"])),
                    trust=TrustLevel(str(raw["trust"])),
                    revision=revision,
                    scope=_strings(raw["scope"], name="instruction scope"),
                    expiry=str(raw["expiry"]) if raw["expiry"] is not None else None,
                )
            )
        overrides: list[TaskOverride] = []
        for raw in _objects(payload["overrides"], name="overrides"):
            _required_fields(raw, {"override_id", "rule_id", "scope", "reason", "actor", "expiry"})
            overrides.append(
                TaskOverride(
                    override_id=str(raw["override_id"]),
                    rule_id=str(raw["rule_id"]),
                    scope=_strings(raw["scope"], name="override scope"),
                    reason=str(raw["reason"]),
                    actor=str(raw["actor"]),
                    expiry=str(raw["expiry"]) if raw["expiry"] is not None else None,
                )
            )
        task_revision = payload["task_revision"]
        if not isinstance(task_revision, int) or isinstance(task_revision, bool):
            raise ValueError("task_revision must be an integer")
        mutation_count = payload["mutation_count"]
        if not isinstance(mutation_count, int) or isinstance(mutation_count, bool):
            raise ValueError("mutation_count must be an integer")

        return TaskCapsule(
            task_id=str(payload["task_id"]),
            state=TaskState(str(payload["state"])),
            intent=str(payload["intent"]),
            acceptance_criteria=tuple(criteria),
            constraints=_strings(payload["constraints"], name="constraints"),
            repo_ids=_strings(payload["repo_ids"], name="repo_ids"),
            workspace_bindings=tuple(bindings),
            source_issue_or_pr=(
                str(payload["source_issue_or_pr"])
                if payload["source_issue_or_pr"] is not None
                else None
            ),
            active_config_generation=generation,
            accepted_plan_id=(
                str(payload["accepted_plan_id"])
                if payload["accepted_plan_id"] is not None
                else None
            ),
            decisions=tuple(decisions),
            evidence_snapshot_ids=_strings(
                payload["evidence_snapshot_ids"], name="evidence_snapshot_ids"
            ),
            receipt_ids=_strings(payload["receipt_ids"], name="receipt_ids"),
            current_phase=str(payload["current_phase"]),
            blocked_reason=(
                str(payload["blocked_reason"]) if payload["blocked_reason"] is not None else None
            ),
            open_questions=tuple(questions),
            next_safe_actions=tuple(actions),
            created_at=str(payload["created_at"]),
            updated_at=str(payload["updated_at"]),
            principal=str(payload["principal"]),
            path_scope=_strings(payload["path_scope"], name="path_scope"),
            instructions=tuple(instructions),
            overrides=tuple(overrides),
            task_revision=task_revision,
            guides_delivered=_strings(payload["guides_delivered"], name="guides_delivered"),
            escalated_rules=_strings(payload["escalated_rules"], name="escalated_rules"),
            mutation_count=mutation_count,
            lease_holder=str(payload["lease_holder"])
            if payload["lease_holder"] is not None
            else None,
            lease_expires_at=(
                str(payload["lease_expires_at"])
                if payload["lease_expires_at"] is not None
                else None
            ),
        )


class JsonTaskStore:
    def __init__(self, state_root: Path, locks: LockManager) -> None:
        self._repository = JsonStateRepository[TaskCapsule](
            state_root,
            collection=TASK_CAPSULES_COLLECTION,
            locks=locks,
            codec=TaskCapsuleCodec(),
            id_validator=validate_task_id,
            max_record_bytes=1_000_000,
        )
        self.root = self._repository.root

    def create(self, task: TaskCapsule) -> StateEnvelope[TaskCapsule]:
        return self._repository.create(task.task_id, task)

    def read(self, task_id: str) -> StateEnvelope[TaskCapsule] | None:
        return self._repository.read(task_id)

    def save(self, task: TaskCapsule, *, expected_revision: Revision) -> StateEnvelope[TaskCapsule]:
        return self._repository.save(task.task_id, task, expected_revision=expected_revision)

    def list_records(self, *, max_records: int) -> StatePage[TaskCapsule]:
        return self._repository.list_records(max_records=max_records)

    def delete(self, task_id: str) -> None:
        self._repository.delete(task_id)
