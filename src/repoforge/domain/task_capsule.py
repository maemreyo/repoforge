"""Typed durable task intent and compact resume contracts."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any

TASK_CAPSULE_SCHEMA_VERSION = 1
_TASK_ID = re.compile(r"^task-[a-f0-9]{24}$")
_VALUE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/#-]{0,127}$")
_GIT_OID = re.compile(r"^[a-f0-9]{40,64}$")
_SHA256 = re.compile(r"^[a-f0-9]{64}$")


def _text(name: str, value: str, *, limit: int, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    normalized = value.strip()
    if (not normalized and not allow_empty) or len(normalized) > limit:
        raise ValueError(f"{name} must contain between 1 and {limit} characters")
    if any(ord(character) < 32 and character not in "\t\n" for character in normalized):
        raise ValueError(f"{name} contains control characters")
    return normalized


def _identifier(name: str, value: str) -> str:
    if not isinstance(value, str) or _VALUE_ID.fullmatch(value) is None:
        raise ValueError(f"{name} is invalid")
    return value


def _identifiers(name: str, values: tuple[str, ...], *, limit: int) -> tuple[str, ...]:
    if len(values) > limit:
        raise ValueError(f"{name} exceeds its {limit}-item bound")
    normalized = tuple(_identifier(name, value) for value in values)
    if len(set(normalized)) != len(normalized):
        raise ValueError(f"{name} contains duplicate identifiers")
    return normalized


class TaskState(str, Enum):
    DRAFT = "draft"
    READY = "ready"
    ACTIVE = "active"
    BLOCKED = "blocked"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"
    COMPLETED = "completed"
    FAILED = "failed"


class CriterionStatus(str, Enum):
    PENDING = "pending"
    PASSED = "passed"
    FAILED = "failed"
    WAIVED = "waived"


_TERMINAL = frozenset({TaskState.CANCELLED, TaskState.COMPLETED, TaskState.FAILED})
_TRANSITIONS: dict[TaskState, frozenset[TaskState]] = {
    TaskState.DRAFT: frozenset({TaskState.READY, TaskState.CANCELLED, TaskState.FAILED}),
    TaskState.READY: frozenset({TaskState.ACTIVE, TaskState.CANCELLED, TaskState.FAILED}),
    TaskState.ACTIVE: frozenset(
        {TaskState.BLOCKED, TaskState.CANCELLING, TaskState.COMPLETED, TaskState.FAILED}
    ),
    TaskState.BLOCKED: frozenset({TaskState.ACTIVE, TaskState.CANCELLING, TaskState.FAILED}),
    TaskState.CANCELLING: frozenset({TaskState.CANCELLED, TaskState.FAILED}),
    TaskState.CANCELLED: frozenset(),
    TaskState.COMPLETED: frozenset(),
    TaskState.FAILED: frozenset(),
}


@dataclass(frozen=True, slots=True)
class TaskCriterion:
    criterion_id: str
    summary: str
    status: CriterionStatus = CriterionStatus.PENDING
    evidence_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _identifier("criterion_id", self.criterion_id)
        _text("criterion summary", self.summary, limit=500)
        _identifiers("criterion evidence_ids", self.evidence_ids, limit=32)
        if self.status is CriterionStatus.PASSED and not self.evidence_ids:
            raise ValueError("passed acceptance criteria require evidence identifiers")


@dataclass(frozen=True, slots=True)
class TaskDecision:
    decision_id: str
    summary: str
    outcome: str
    decided_at: str

    def __post_init__(self) -> None:
        _identifier("decision_id", self.decision_id)
        _text("decision summary", self.summary, limit=500)
        _text("decision outcome", self.outcome, limit=500)
        _text("decision timestamp", self.decided_at, limit=64)


@dataclass(frozen=True, slots=True)
class TaskQuestion:
    question_id: str
    summary: str

    def __post_init__(self) -> None:
        _identifier("question_id", self.question_id)
        _text("question summary", self.summary, limit=500)


@dataclass(frozen=True, slots=True)
class TaskAction:
    action: str
    reason: str
    required: bool = False

    def __post_init__(self) -> None:
        _identifier("task action", self.action)
        _text("task action reason", self.reason, limit=500)


@dataclass(frozen=True, slots=True)
class WorkspaceBinding:
    workspace_id: str
    repo_id: str
    head_sha: str | None = None
    workspace_fingerprint: str | None = None
    stale: bool = False

    def __post_init__(self) -> None:
        _identifier("workspace_id", self.workspace_id)
        _identifier("repo_id", self.repo_id)
        if self.head_sha is not None and _GIT_OID.fullmatch(self.head_sha) is None:
            raise ValueError("workspace binding head_sha must be a Git object identity")
        if (
            self.workspace_fingerprint is not None
            and _SHA256.fullmatch(self.workspace_fingerprint) is None
        ):
            raise ValueError("workspace binding fingerprint must be a SHA-256 identity")


@dataclass(frozen=True, slots=True)
class TaskCapsule:
    task_id: str
    state: TaskState
    intent: str
    acceptance_criteria: tuple[TaskCriterion, ...]
    constraints: tuple[str, ...]
    repo_ids: tuple[str, ...]
    workspace_bindings: tuple[WorkspaceBinding, ...]
    source_issue_or_pr: str | None
    active_config_generation: int | None
    accepted_plan_id: str | None
    decisions: tuple[TaskDecision, ...]
    evidence_snapshot_ids: tuple[str, ...]
    receipt_ids: tuple[str, ...]
    current_phase: str
    blocked_reason: str | None
    open_questions: tuple[TaskQuestion, ...]
    next_safe_actions: tuple[TaskAction, ...]
    created_at: str
    updated_at: str

    def __post_init__(self) -> None:
        validate_task_id(self.task_id)
        _text("task intent", self.intent, limit=2_000)
        if not self.acceptance_criteria or len(self.acceptance_criteria) > 64:
            raise ValueError("task acceptance criteria must contain between 1 and 64 items")
        criterion_ids = [criterion.criterion_id for criterion in self.acceptance_criteria]
        if len(set(criterion_ids)) != len(criterion_ids):
            raise ValueError("task acceptance criteria contain duplicate identifiers")
        if len(self.constraints) > 64:
            raise ValueError("task constraints exceed their 64-item bound")
        for constraint in self.constraints:
            _text("task constraint", constraint, limit=500)
        _identifiers("task repo_ids", self.repo_ids, limit=16)
        if len(self.workspace_bindings) > 32:
            raise ValueError("workspace bindings exceed their 32-item bound")
        workspace_ids = [binding.workspace_id for binding in self.workspace_bindings]
        if len(set(workspace_ids)) != len(workspace_ids):
            raise ValueError("workspace bindings contain duplicate workspace ids")
        if self.source_issue_or_pr is not None:
            _text("source issue or PR", self.source_issue_or_pr, limit=256)
        if self.active_config_generation is not None and (
            not isinstance(self.active_config_generation, int)
            or isinstance(self.active_config_generation, bool)
            or self.active_config_generation <= 0
        ):
            raise ValueError("active_config_generation must be a positive integer")
        if self.accepted_plan_id is not None:
            _identifier("accepted_plan_id", self.accepted_plan_id)
        if len(self.decisions) > 128:
            raise ValueError("task decisions exceed their 128-item bound")
        decision_ids = [decision.decision_id for decision in self.decisions]
        if len(set(decision_ids)) != len(decision_ids):
            raise ValueError("task decisions contain duplicate identifiers")
        _identifiers("evidence_snapshot_ids", self.evidence_snapshot_ids, limit=256)
        _identifiers("receipt_ids", self.receipt_ids, limit=256)
        _text("current_phase", self.current_phase, limit=128)
        if self.blocked_reason is not None:
            _text("blocked_reason", self.blocked_reason, limit=1_000)
        if self.state is TaskState.BLOCKED and self.blocked_reason is None:
            raise ValueError("blocked task state requires a blocked_reason")
        if self.state is not TaskState.BLOCKED and self.blocked_reason is not None:
            raise ValueError("blocked_reason is only valid for blocked task state")
        if len(self.open_questions) > 64:
            raise ValueError("open questions exceed their 64-item bound")
        question_ids = [question.question_id for question in self.open_questions]
        if len(set(question_ids)) != len(question_ids):
            raise ValueError("open questions contain duplicate identifiers")
        if len(self.next_safe_actions) > 32:
            raise ValueError("next safe actions exceed their 32-item bound")
        _text("created_at", self.created_at, limit=64)
        _text("updated_at", self.updated_at, limit=64)
        if self.state is TaskState.COMPLETED:
            _assert_completion_ready(self)

    @classmethod
    def new(
        cls,
        *,
        task_id: str,
        intent: str,
        acceptance_criteria: tuple[str, ...],
        constraints: tuple[str, ...],
        repo_ids: tuple[str, ...],
        created_at: str,
    ) -> TaskCapsule:
        criteria = tuple(
            TaskCriterion(f"criterion-{index}", summary)
            for index, summary in enumerate(acceptance_criteria, start=1)
        )
        return cls(
            task_id=task_id,
            state=TaskState.DRAFT,
            intent=intent,
            acceptance_criteria=criteria,
            constraints=constraints,
            repo_ids=repo_ids,
            workspace_bindings=(),
            source_issue_or_pr=None,
            active_config_generation=None,
            accepted_plan_id=None,
            decisions=(),
            evidence_snapshot_ids=(),
            receipt_ids=(),
            current_phase="intake",
            blocked_reason=None,
            open_questions=(),
            next_safe_actions=(),
            created_at=created_at,
            updated_at=created_at,
        )

    def resume_projection(self) -> dict[str, object]:
        criteria = Counter(criterion.status.value for criterion in self.acceptance_criteria)
        return {
            "task_id": self.task_id,
            "state": self.state.value,
            "intent": self.intent,
            "current_phase": self.current_phase,
            "blocked_reason": self.blocked_reason,
            "criteria": {key: criteria[key] for key in sorted(criteria) if criteria[key]},
            "repo_ids": list(self.repo_ids),
            "workspaces": [
                {
                    "workspace_id": binding.workspace_id,
                    "repo_id": binding.repo_id,
                    "stale": binding.stale,
                }
                for binding in self.workspace_bindings
            ],
            "decisions": [
                {
                    "decision_id": decision.decision_id,
                    "summary": decision.summary,
                    "outcome": decision.outcome,
                }
                for decision in self.decisions[-20:]
            ],
            "open_questions": [
                {"question_id": question.question_id, "summary": question.summary}
                for question in self.open_questions
            ],
            "next_safe_actions": [
                {
                    "action": action.action,
                    "reason": action.reason,
                    "required": action.required,
                }
                for action in self.next_safe_actions
            ],
            "updated_at": self.updated_at,
        }


def validate_task_id(value: str) -> str:
    if not isinstance(value, str) or _TASK_ID.fullmatch(value) is None:
        raise ValueError("task_id must use task- followed by 24 lowercase hexadecimal characters")
    return value


def _assert_completion_ready(task: TaskCapsule) -> None:
    if any(criterion.status is CriterionStatus.PENDING for criterion in task.acceptance_criteria):
        raise ValueError("completed tasks require all acceptance criteria to be disposed")
    if task.open_questions:
        raise ValueError("completed tasks cannot retain open questions")
    if task.blocked_reason is not None:
        raise ValueError("completed tasks cannot retain a blocked reason")


def replace_task(task: TaskCapsule, **changes: Any) -> TaskCapsule:
    """Return a validated immutable task replacement."""

    return replace(task, **changes)


def transition_task(task: TaskCapsule, state: TaskState, *, updated_at: str) -> TaskCapsule:
    """Apply one explicit idempotent task-state transition."""

    _text("updated_at", updated_at, limit=64)
    if task.state is state:
        return replace(task, updated_at=updated_at)
    if task.state in _TERMINAL:
        raise ValueError(f"terminal task state {task.state.value} cannot transition")
    if state not in _TRANSITIONS[task.state]:
        raise ValueError(f"invalid task transition: {task.state.value} -> {state.value}")
    if state is TaskState.COMPLETED:
        _assert_completion_ready(task)
    blocked_reason = task.blocked_reason if state is TaskState.BLOCKED else None
    if state is TaskState.BLOCKED and blocked_reason is None:
        raise ValueError("transition to blocked requires blocked_reason to be set first")
    return replace(task, state=state, blocked_reason=blocked_reason, updated_at=updated_at)
