"""Tests for ``RuntimeTransition`` — 14-state typed transition model with instance methods."""

from __future__ import annotations

import pytest

from repoforge.domain.errors import ErrorCode, RepoForgeError
from repoforge.domain.runtime_transition import (
    RuntimeTransition,
    RuntimeTransitionStatus,
    is_terminal,
    new_runtime_transition,
)

_TID = "tran-000000000000000000000001"
_ISO = "2026-01-01T00:00:00+00:00"
_ISO2 = "2026-01-01T00:00:01+00:00"


def _transition(**overrides: object) -> RuntimeTransition:
    """Build a minimal PREPARED RuntimeTransition with overridable fields."""
    return new_runtime_transition(
        transition_id=_TID,
        target_generation=1,
        correlation_id="corr-001",
        started_at=_ISO,
        **{
            k: v
            for k, v in overrides.items()
            if k in ("config_generation", "previous_transition_id")
        },  # type: ignore[arg-type]
    )


# --------------------------------------------------------------------------- factory


class TestNewRuntimeTransition:
    """Given new_runtime_transition(), when called, then it creates a PREPARED transition."""

    def test_creates_in_prepared_state(self) -> None:
        t = _transition()
        assert t.status is RuntimeTransitionStatus.PREPARED
        assert not t.is_terminal

    def test_sets_updated_at_equal_to_started_at(self) -> None:
        t = _transition()
        assert t.updated_at == _ISO

    def test_completed_at_is_none(self) -> None:
        t = _transition()
        assert t.completed_at is None

    def test_error_fields_are_none(self) -> None:
        t = _transition()
        assert t.error_code is None
        assert t.error_message is None

    def test_sets_previous_transition_id_when_given(self) -> None:
        t = _transition(previous_transition_id="tran-000000000000000000000000")
        assert t.previous_transition_id == "tran-000000000000000000000000"

    def test_accepts_config_generation(self) -> None:
        t = _transition(config_generation=5)
        assert t.config_generation == 5


# --------------------------------------------------------------------------- 14 transitions (typed instance methods)


class TestAllTransitions:
    """Given a RuntimeTransition in the right status, when calling each typed method, then it advances to the target."""

    def test_prepare_transition(self) -> None:
        t = _transition()
        # PREPARED -> CONFIG_GENERATED -> CONFIG_FAILED -> PREPARED (retry)
        t = t.config_generated(updated_at=_ISO2)
        t = t.fail_config(updated_at=_ISO2, error_code="CFG_ERR", error_message="config failed")
        t = t.prepare(updated_at=_ISO2)
        assert t.status is RuntimeTransitionStatus.PREPARED

    def test_config_generated(self) -> None:
        t = _transition()
        t2 = t.config_generated(updated_at=_ISO2)
        assert t2.status is RuntimeTransitionStatus.CONFIG_GENERATED

    def test_resolve_dependencies(self) -> None:
        t = _transition()
        t2 = t.config_generated(updated_at=_ISO2)
        t3 = t2.resolve_dependencies(updated_at=_ISO2)
        assert t3.status is RuntimeTransitionStatus.DEPENDENCIES_RESOLVED

    def test_validate(self) -> None:
        t = _transition()
        t2 = t.config_generated(updated_at=_ISO2)
        t3 = t2.resolve_dependencies(updated_at=_ISO2)
        t4 = t3.validate(updated_at=_ISO2)
        assert t4.status is RuntimeTransitionStatus.VALIDATED

    def test_stage(self) -> None:
        t = _transition()
        t2 = t.config_generated(updated_at=_ISO2)
        t3 = t2.resolve_dependencies(updated_at=_ISO2)
        t4 = t3.validate(updated_at=_ISO2)
        t5 = t4.stage(updated_at=_ISO2)
        assert t5.status is RuntimeTransitionStatus.STAGED

    def test_activate(self) -> None:
        t = _transition()
        t2 = t.config_generated(updated_at=_ISO2)
        t3 = t2.resolve_dependencies(updated_at=_ISO2)
        t4 = t3.validate(updated_at=_ISO2)
        t5 = t4.stage(updated_at=_ISO2)
        t6 = t5.activate(updated_at=_ISO2)
        assert t6.status is RuntimeTransitionStatus.ACTIVATED

    def test_health_checked(self) -> None:
        t = _transition()
        t2 = t.config_generated(updated_at=_ISO2)
        t3 = t2.resolve_dependencies(updated_at=_ISO2)
        t4 = t3.validate(updated_at=_ISO2)
        t5 = t4.stage(updated_at=_ISO2)
        t6 = t5.activate(updated_at=_ISO2)
        t7 = t6.health_checked(updated_at=_ISO2)
        assert t7.status is RuntimeTransitionStatus.HEALTH_CHECKED

    def test_mark_ready(self) -> None:
        t = _transition()
        t2 = t.config_generated(updated_at=_ISO2)
        t3 = t2.resolve_dependencies(updated_at=_ISO2)
        t4 = t3.validate(updated_at=_ISO2)
        t5 = t4.stage(updated_at=_ISO2)
        t6 = t5.activate(updated_at=_ISO2)
        t7 = t6.health_checked(updated_at=_ISO2)
        t8 = t7.mark_ready(updated_at=_ISO2)
        assert t8.status is RuntimeTransitionStatus.READY

    def test_complete(self) -> None:
        t = _transition()
        t2 = t.config_generated(updated_at=_ISO2)
        t3 = t2.resolve_dependencies(updated_at=_ISO2)
        t4 = t3.validate(updated_at=_ISO2)
        t5 = t4.stage(updated_at=_ISO2)
        t6 = t5.activate(updated_at=_ISO2)
        t7 = t6.health_checked(updated_at=_ISO2)
        t8 = t7.mark_ready(updated_at=_ISO2)
        t9 = t8.complete(updated_at=_ISO2)
        assert t9.status is RuntimeTransitionStatus.COMPLETED
        assert t9.completed_at == _ISO2

    def test_fail_config(self) -> None:
        t = _transition()
        t2 = t.fail_config(updated_at=_ISO2, error_code="CFG_ERR", error_message="bad config")
        assert t2.status is RuntimeTransitionStatus.CONFIG_FAILED
        assert t2.error_code == "CFG_ERR"
        assert t2.error_message == "bad config"

    def test_fail_validation(self) -> None:
        t = _transition()
        t2 = t.config_generated(updated_at=_ISO2)
        t3 = t2.resolve_dependencies(updated_at=_ISO2)
        t4 = t3.fail_validation(updated_at=_ISO2, error_code="VAL_ERR", error_message="invalid")
        assert t4.status is RuntimeTransitionStatus.VALIDATION_FAILED

    def test_fail_activation(self) -> None:
        t = _transition()
        t2 = t.config_generated(updated_at=_ISO2)
        t3 = t2.resolve_dependencies(updated_at=_ISO2)
        t4 = t3.validate(updated_at=_ISO2)
        t5 = t4.stage(updated_at=_ISO2)
        t6 = t5.fail_activation(
            updated_at=_ISO2, error_code="ACT_ERR", error_message="activation failed"
        )
        assert t6.status is RuntimeTransitionStatus.ACTIVATION_FAILED

    def test_fail_health(self) -> None:
        t = _transition()
        t2 = t.config_generated(updated_at=_ISO2)
        t3 = t2.resolve_dependencies(updated_at=_ISO2)
        t4 = t3.validate(updated_at=_ISO2)
        t5 = t4.stage(updated_at=_ISO2)
        t6 = t5.activate(updated_at=_ISO2)
        t7 = t6.health_checked(updated_at=_ISO2)
        t8 = t7.fail_health(
            updated_at=_ISO2, error_code="HLTH_ERR", error_message="health check failed"
        )
        assert t8.status is RuntimeTransitionStatus.HEALTH_FAILED

    def test_rollback(self) -> None:
        t = _transition()
        t2 = t.config_generated(updated_at=_ISO2)
        t3 = t2.resolve_dependencies(updated_at=_ISO2)
        t4 = t3.validate(updated_at=_ISO2)
        t5 = t4.stage(updated_at=_ISO2)
        t6 = t5.activate(updated_at=_ISO2)
        t7 = t6.health_checked(updated_at=_ISO2)
        t8 = t7.rollback(updated_at=_ISO2)
        assert t8.status is RuntimeTransitionStatus.ROLLED_BACK
        assert t8.completed_at == _ISO2

    def test_fail_health_from_ready(self) -> None:
        t = _transition()
        t2 = t.config_generated(updated_at=_ISO2)
        t3 = t2.resolve_dependencies(updated_at=_ISO2)
        t4 = t3.validate(updated_at=_ISO2)
        t5 = t4.stage(updated_at=_ISO2)
        t6 = t5.activate(updated_at=_ISO2)
        t7 = t6.health_checked(updated_at=_ISO2)
        t8 = t7.mark_ready(updated_at=_ISO2)
        t9 = t8.fail_health(updated_at=_ISO2, error_code="HLTH_ERR", error_message="degraded")
        assert t9.status is RuntimeTransitionStatus.HEALTH_FAILED

    def test_rollback_from_ready(self) -> None:
        t = _transition()
        t2 = t.config_generated(updated_at=_ISO2)
        t3 = t2.resolve_dependencies(updated_at=_ISO2)
        t4 = t3.validate(updated_at=_ISO2)
        t5 = t4.stage(updated_at=_ISO2)
        t6 = t5.activate(updated_at=_ISO2)
        t7 = t6.health_checked(updated_at=_ISO2)
        t8 = t7.mark_ready(updated_at=_ISO2)
        t9 = t8.rollback(updated_at=_ISO2)
        assert t9.status is RuntimeTransitionStatus.ROLLED_BACK


# --------------------------------------------------------------------------- invalid transitions


class TestInvalidTransitions:
    """Given a RuntimeTransition, when calling an invalid transition, then RepoForgeError is raised."""

    def test_prepare_cannot_skip_to_ready(self) -> None:
        t = _transition()
        with pytest.raises(RepoForgeError) as exc_info:
            t.mark_ready(updated_at=_ISO2)
        assert exc_info.value.code is ErrorCode.OPERATION_TRANSITION_INVALID

    def test_prepared_cannot_activate(self) -> None:
        t = _transition()
        with pytest.raises(RepoForgeError, match="Illegal runtime transition"):
            t.activate(updated_at=_ISO2)

    def test_completed_cannot_transition(self) -> None:
        t = _transition()
        t2 = t.config_generated(updated_at=_ISO2)
        t3 = t2.resolve_dependencies(updated_at=_ISO2)
        t4 = t3.validate(updated_at=_ISO2)
        t5 = t4.stage(updated_at=_ISO2)
        t6 = t5.activate(updated_at=_ISO2)
        t7 = t6.health_checked(updated_at=_ISO2)
        t8 = t7.mark_ready(updated_at=_ISO2)
        t9 = t8.complete(updated_at=_ISO2)
        with pytest.raises(RepoForgeError, match="Illegal runtime transition"):
            t9.prepare(updated_at=_ISO2)

    def test_rolled_back_cannot_transition(self) -> None:
        t = _transition()
        t2 = t.config_generated(updated_at=_ISO2)
        t3 = t2.resolve_dependencies(updated_at=_ISO2)
        t4 = t3.validate(updated_at=_ISO2)
        t5 = t4.stage(updated_at=_ISO2)
        t6 = t5.activate(updated_at=_ISO2)
        t7 = t6.health_checked(updated_at=_ISO2)
        t8 = t7.rollback(updated_at=_ISO2)
        with pytest.raises(RepoForgeError, match="Illegal runtime transition"):
            t8.config_generated(updated_at=_ISO2)

    def test_config_generated_cannot_validate(self) -> None:
        t = _transition()
        t2 = t.config_generated(updated_at=_ISO2)
        with pytest.raises(RepoForgeError, match="Illegal runtime transition"):
            t2.validate(updated_at=_ISO2)

    def test_validation_failed_recover_path(self) -> None:
        t = _transition()
        t2 = t.config_generated(updated_at=_ISO2)
        t3 = t2.resolve_dependencies(updated_at=_ISO2)
        t4 = t3.fail_validation(updated_at=_ISO2, error_code="BAD", error_message="bad")
        # VALIDATION_FAILED -> DEPENDENCIES_RESOLVED is valid
        t5 = t4.resolve_dependencies(updated_at=_ISO2)
        assert t5.status is RuntimeTransitionStatus.DEPENDENCIES_RESOLVED

    def test_health_failed_recover_path(self) -> None:
        t = _transition()
        t2 = t.config_generated(updated_at=_ISO2)
        t3 = t2.resolve_dependencies(updated_at=_ISO2)
        t4 = t3.validate(updated_at=_ISO2)
        t5 = t4.stage(updated_at=_ISO2)
        t6 = t5.activate(updated_at=_ISO2)
        t7 = t6.health_checked(updated_at=_ISO2)
        t8 = t7.fail_health(updated_at=_ISO2, error_code="BAD", error_message="bad")
        # HEALTH_FAILED -> ACTIVATED is valid (recovery path)
        t9 = t8.activate(updated_at=_ISO2)
        assert t9.status is RuntimeTransitionStatus.ACTIVATED


# --------------------------------------------------------------------------- is_terminal


class TestIsTerminal:
    """Given is_terminal(), when checking various states, then it returns correctly."""

    def test_completed_is_terminal(self) -> None:
        assert is_terminal(RuntimeTransitionStatus.COMPLETED)

    def test_rolled_back_is_terminal(self) -> None:
        assert is_terminal(RuntimeTransitionStatus.ROLLED_BACK)

    def test_prepared_is_not_terminal(self) -> None:
        assert not is_terminal(RuntimeTransitionStatus.PREPARED)

    def test_config_generated_is_not_terminal(self) -> None:
        assert not is_terminal(RuntimeTransitionStatus.CONFIG_GENERATED)

    def test_health_failed_is_not_terminal(self) -> None:
        assert not is_terminal(RuntimeTransitionStatus.HEALTH_FAILED)

    def test_instance_property_reflects_module_function(self) -> None:
        t = _transition()
        assert t.is_terminal == is_terminal(t.status)
        # after advance to COMPLETED
        t2 = t.config_generated(updated_at=_ISO2)
        t3 = t2.resolve_dependencies(updated_at=_ISO2)
        t4 = t3.validate(updated_at=_ISO2)
        t5 = t4.stage(updated_at=_ISO2)
        t6 = t5.activate(updated_at=_ISO2)
        t7 = t6.health_checked(updated_at=_ISO2)
        t8 = t7.mark_ready(updated_at=_ISO2)
        t9 = t8.complete(updated_at=_ISO2)
        assert t9.is_terminal


# --------------------------------------------------------------------------- __post_init__ validation


class TestPostInitValidation:
    """Given invalid constructor values, when creating a RuntimeTransition, then ValueError is raised."""

    def test_rejects_invalid_transition_id(self) -> None:
        with pytest.raises(ValueError, match="invalid transition id"):
            RuntimeTransition(
                transition_id="bad",
                status=RuntimeTransitionStatus.PREPARED,
                target_generation=1,
                config_generation=None,
                correlation_id="corr-001",
                started_at=_ISO,
                updated_at=_ISO,
                completed_at=None,
                error_code=None,
                error_message=None,
                previous_transition_id=None,
            )

    def test_rejects_zero_target_generation(self) -> None:
        with pytest.raises(ValueError, match="target_generation"):
            RuntimeTransition(
                transition_id=_TID,
                status=RuntimeTransitionStatus.PREPARED,
                target_generation=0,
                config_generation=None,
                correlation_id="corr-001",
                started_at=_ISO,
                updated_at=_ISO,
                completed_at=None,
                error_code=None,
                error_message=None,
                previous_transition_id=None,
            )

    def test_rejects_negative_target_generation(self) -> None:
        with pytest.raises(ValueError, match="target_generation"):
            RuntimeTransition(
                transition_id=_TID,
                status=RuntimeTransitionStatus.PREPARED,
                target_generation=-1,
                config_generation=None,
                correlation_id="corr-001",
                started_at=_ISO,
                updated_at=_ISO,
                completed_at=None,
                error_code=None,
                error_message=None,
                previous_transition_id=None,
            )

    def test_rejects_zero_config_generation(self) -> None:
        with pytest.raises(ValueError, match="config_generation"):
            RuntimeTransition(
                transition_id=_TID,
                status=RuntimeTransitionStatus.PREPARED,
                target_generation=1,
                config_generation=0,
                correlation_id="corr-001",
                started_at=_ISO,
                updated_at=_ISO,
                completed_at=None,
                error_code=None,
                error_message=None,
                previous_transition_id=None,
            )

    def test_rejects_empty_correlation_id(self) -> None:
        with pytest.raises(ValueError, match="correlation_id"):
            RuntimeTransition(
                transition_id=_TID,
                status=RuntimeTransitionStatus.PREPARED,
                target_generation=1,
                config_generation=None,
                correlation_id="",
                started_at=_ISO,
                updated_at=_ISO,
                completed_at=None,
                error_code=None,
                error_message=None,
                previous_transition_id=None,
            )

    def test_rejects_bad_timestamp(self) -> None:
        with pytest.raises(ValueError, match="started_at"):
            RuntimeTransition(
                transition_id=_TID,
                status=RuntimeTransitionStatus.PREPARED,
                target_generation=1,
                config_generation=None,
                correlation_id="corr-001",
                started_at="not-a-timestamp",
                updated_at=_ISO,
                completed_at=None,
                error_code=None,
                error_message=None,
                previous_transition_id=None,
            )

    def test_rejects_naive_timestamp(self) -> None:
        """ISO timestamps must include a timezone offset."""
        with pytest.raises(ValueError, match="timezone"):
            RuntimeTransition(
                transition_id=_TID,
                status=RuntimeTransitionStatus.PREPARED,
                target_generation=1,
                config_generation=None,
                correlation_id="corr-001",
                started_at="2026-01-01T00:00:00",
                updated_at=_ISO,
                completed_at=None,
                error_code=None,
                error_message=None,
                previous_transition_id=None,
            )

    def test_rejects_invalid_previous_transition_id(self) -> None:
        with pytest.raises(ValueError, match="invalid transition id"):
            RuntimeTransition(
                transition_id=_TID,
                status=RuntimeTransitionStatus.PREPARED,
                target_generation=1,
                config_generation=None,
                correlation_id="corr-001",
                started_at=_ISO,
                updated_at=_ISO,
                completed_at=None,
                error_code=None,
                error_message=None,
                previous_transition_id="bad-id",
            )


# --------------------------------------------------------------------------- immutability


class TestImmutability:
    """Given a RuntimeTransition (frozen dataclass), when trying to set an attribute, it is rejected."""

    def test_cannot_set_status_directly(self) -> None:
        t = _transition()
        with pytest.raises(AttributeError):
            t.status = RuntimeTransitionStatus.COMPLETED  # type: ignore[misc]

    def test_cannot_set_target_generation_directly(self) -> None:
        t = _transition()
        with pytest.raises(AttributeError):
            t.target_generation = 2  # type: ignore[misc]
