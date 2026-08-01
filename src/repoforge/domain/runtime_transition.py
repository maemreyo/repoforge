"""Runtime activation/boot lifecycle — 14-state transition model.

Forward (9): PREPARED → CONFIG_GENERATED → DEPENDENCIES_RESOLVED → VALIDATED
            → STAGED → ACTIVATED → HEALTH_CHECKED → READY → COMPLETED.
Failure (5): CONFIG_FAILED, VALIDATION_FAILED, ACTIVATION_FAILED,
            HEALTH_FAILED, ROLLED_BACK.

No generic ``transition(status)`` setter: every advance is a typed method
that validates against ``_ALLOWED_TRANSITIONS`` and returns a new frozen
instance.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum

from .errors import ErrorCode, RepoForgeError

_TRANSITION_ID = re.compile(r"^tran-[a-f0-9]{24}$")
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")


# --------------------------------------------------------------------------- status


class RuntimeTransitionStatus(str, Enum):
    PREPARED = "prepared"
    CONFIG_GENERATED = "config_generated"
    DEPENDENCIES_RESOLVED = "dependencies_resolved"
    VALIDATED = "validated"
    STAGED = "staged"
    ACTIVATED = "activated"
    HEALTH_CHECKED = "health_checked"
    READY = "ready"
    COMPLETED = "completed"
    CONFIG_FAILED = "config_failed"
    VALIDATION_FAILED = "validation_failed"
    ACTIVATION_FAILED = "activation_failed"
    HEALTH_FAILED = "health_failed"
    ROLLED_BACK = "rolled_back"


_S = RuntimeTransitionStatus

_ALLOWED: dict[RuntimeTransitionStatus, frozenset[RuntimeTransitionStatus]] = {
    _S.PREPARED: frozenset({_S.CONFIG_GENERATED, _S.CONFIG_FAILED}),
    _S.CONFIG_GENERATED: frozenset({_S.DEPENDENCIES_RESOLVED, _S.CONFIG_FAILED}),
    _S.DEPENDENCIES_RESOLVED: frozenset({_S.VALIDATED, _S.VALIDATION_FAILED}),
    _S.VALIDATED: frozenset({_S.STAGED, _S.VALIDATION_FAILED}),
    _S.STAGED: frozenset({_S.ACTIVATED, _S.ACTIVATION_FAILED}),
    _S.ACTIVATED: frozenset({_S.HEALTH_CHECKED, _S.ACTIVATION_FAILED}),
    _S.HEALTH_CHECKED: frozenset({_S.READY, _S.HEALTH_FAILED, _S.ROLLED_BACK}),
    _S.READY: frozenset({_S.COMPLETED, _S.HEALTH_FAILED, _S.ROLLED_BACK}),
    _S.COMPLETED: frozenset(),
    _S.CONFIG_FAILED: frozenset({_S.PREPARED}),
    _S.VALIDATION_FAILED: frozenset({_S.DEPENDENCIES_RESOLVED}),
    _S.ACTIVATION_FAILED: frozenset({_S.STAGED}),
    _S.HEALTH_FAILED: frozenset({_S.ACTIVATED, _S.ROLLED_BACK}),
    _S.ROLLED_BACK: frozenset(),
}

_TERMINAL = frozenset({_S.COMPLETED, _S.ROLLED_BACK})


def is_terminal(state: RuntimeTransitionStatus) -> bool:
    return state in _TERMINAL


# --------------------------------------------------------------------------- validators


def _transition_id(value: str) -> str:
    if _TRANSITION_ID.fullmatch(value) is None:
        raise ValueError(f"invalid transition id: {value!r}")
    return value


def _positive_int(value: int, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _non_empty_str(value: str, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be a non-empty string")
    return value


def _optional_str(value: str | None, field: str) -> str | None:
    if value is not None and not isinstance(value, str):
        raise ValueError(f"{field} must be a string or None")
    return value


def _safe_id(value: str | None, field: str) -> str | None:
    if value is None:
        return None
    if _SAFE_ID.fullmatch(value) is None:
        raise ValueError(f"{field} is not a valid safe identifier")
    return value


def _iso(value: str, field: str) -> str:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include a timezone offset")
    return value


# --------------------------------------------------------------------------- model


@dataclass(frozen=True, slots=True)
class RuntimeTransition:
    transition_id: str
    status: RuntimeTransitionStatus
    target_generation: int
    config_generation: int | None
    correlation_id: str
    started_at: str
    updated_at: str
    completed_at: str | None
    error_code: str | None
    error_message: str | None
    previous_transition_id: str | None

    def __post_init__(self) -> None:
        _transition_id(self.transition_id)
        _positive_int(self.target_generation, "target_generation")
        if self.config_generation is not None:
            _positive_int(self.config_generation, "config_generation")
        _non_empty_str(self.correlation_id, "correlation_id")
        _iso(self.started_at, "started_at")
        _iso(self.updated_at, "updated_at")
        _optional_str(self.completed_at, "completed_at")
        if self.completed_at is not None:
            _iso(self.completed_at, "completed_at")
        _safe_id(self.error_code, "error_code")
        _optional_str(self.error_message, "error_message")
        if self.previous_transition_id is not None:
            _transition_id(self.previous_transition_id)

    def _validate(self, target: RuntimeTransitionStatus) -> None:
        allowed = _ALLOWED.get(self.status)
        if allowed is None or target not in allowed:
            raise RepoForgeError(
                f"Illegal runtime transition: {self.status.value} → {target.value} "
                f"(id={self.transition_id})",
                code=ErrorCode.OPERATION_TRANSITION_INVALID,
            )

    def _advance(
        self,
        target: RuntimeTransitionStatus,
        *,
        updated_at: str,
        completed_at: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        clear_error: bool = False,
    ) -> RuntimeTransition:
        self._validate(target)
        return replace(
            self,
            status=target,
            updated_at=updated_at,
            completed_at=completed_at,
            error_code=None if clear_error else error_code,
            error_message=None if clear_error else error_message,
        )

    def prepare(self, *, updated_at: str) -> RuntimeTransition:
        return self._advance(_S.PREPARED, updated_at=updated_at, clear_error=True)

    def config_generated(self, *, updated_at: str) -> RuntimeTransition:
        return self._advance(_S.CONFIG_GENERATED, updated_at=updated_at)

    def resolve_dependencies(self, *, updated_at: str) -> RuntimeTransition:
        return self._advance(_S.DEPENDENCIES_RESOLVED, updated_at=updated_at)

    def validate(self, *, updated_at: str) -> RuntimeTransition:
        return self._advance(_S.VALIDATED, updated_at=updated_at)

    def stage(self, *, updated_at: str) -> RuntimeTransition:
        return self._advance(_S.STAGED, updated_at=updated_at)

    def activate(self, *, updated_at: str) -> RuntimeTransition:
        return self._advance(_S.ACTIVATED, updated_at=updated_at)

    def health_checked(self, *, updated_at: str) -> RuntimeTransition:
        return self._advance(_S.HEALTH_CHECKED, updated_at=updated_at)

    def mark_ready(self, *, updated_at: str) -> RuntimeTransition:
        return self._advance(_S.READY, updated_at=updated_at)

    def complete(self, *, updated_at: str) -> RuntimeTransition:
        return self._advance(_S.COMPLETED, updated_at=updated_at, completed_at=updated_at)

    def fail_config(
        self, *, updated_at: str, error_code: str, error_message: str
    ) -> RuntimeTransition:
        return self._advance(
            _S.CONFIG_FAILED,
            updated_at=updated_at,
            error_code=error_code,
            error_message=error_message,
        )

    def fail_validation(
        self, *, updated_at: str, error_code: str, error_message: str
    ) -> RuntimeTransition:
        return self._advance(
            _S.VALIDATION_FAILED,
            updated_at=updated_at,
            error_code=error_code,
            error_message=error_message,
        )

    def fail_activation(
        self, *, updated_at: str, error_code: str, error_message: str
    ) -> RuntimeTransition:
        return self._advance(
            _S.ACTIVATION_FAILED,
            updated_at=updated_at,
            error_code=error_code,
            error_message=error_message,
        )

    def fail_health(
        self, *, updated_at: str, error_code: str, error_message: str
    ) -> RuntimeTransition:
        return self._advance(
            _S.HEALTH_FAILED,
            updated_at=updated_at,
            error_code=error_code,
            error_message=error_message,
        )

    def rollback(self, *, updated_at: str) -> RuntimeTransition:
        return self._advance(_S.ROLLED_BACK, updated_at=updated_at, completed_at=updated_at)

    @property
    def is_terminal(self) -> bool:
        return is_terminal(self.status)


# --------------------------------------------------------------------------- factory


def new_runtime_transition(
    transition_id: str,
    *,
    target_generation: int,
    correlation_id: str,
    started_at: str,
    config_generation: int | None = None,
    previous_transition_id: str | None = None,
) -> RuntimeTransition:
    """Create a new transition in PREPARED state."""
    return RuntimeTransition(
        transition_id=transition_id,
        status=_S.PREPARED,
        target_generation=target_generation,
        config_generation=config_generation,
        correlation_id=correlation_id,
        started_at=started_at,
        updated_at=started_at,
        completed_at=None,
        error_code=None,
        error_message=None,
        previous_transition_id=previous_transition_id,
    )


__all__ = [
    "RuntimeTransition",
    "RuntimeTransitionStatus",
    "is_terminal",
    "new_runtime_transition",
]
