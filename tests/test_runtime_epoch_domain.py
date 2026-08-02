"""Tests for ``RuntimeEpoch`` domain model — monotonic generation counter."""

from __future__ import annotations

import pytest

from repoforge.domain.runtime_epoch import (
    EpochGeneration,
    RuntimeEpoch,
    initial_epoch,
    next_epoch,
)

_CORRELATION_ID = "transition-001"
_CREATED_AT = "2026-01-15T10:00:00+00:00"


# --------------------------------------------------------------------------- initial_epoch


class TestInitialEpoch:
    """Given initial_epoch(), when called with valid params, then it creates generation=1."""

    def test_generation_is_one(self) -> None:
        epoch = initial_epoch(created_at=_CREATED_AT, correlation_id=_CORRELATION_ID)
        assert epoch.generation == 1

    def test_with_label(self) -> None:
        epoch = initial_epoch(
            created_at=_CREATED_AT,
            correlation_id=_CORRELATION_ID,
            label="initial-deployment",
        )
        assert epoch.label == "initial-deployment"
        assert epoch.generation == 1

    def test_label_defaults_to_none(self) -> None:
        epoch = initial_epoch(created_at=_CREATED_AT, correlation_id=_CORRELATION_ID)
        assert epoch.label is None


# --------------------------------------------------------------------------- next_epoch


class TestNextEpoch:
    """Given a current epoch and next_epoch(), when called, then generation increments by 1."""

    def test_increments_generation(self) -> None:
        first = initial_epoch(created_at=_CREATED_AT, correlation_id=_CORRELATION_ID)
        second = next_epoch(
            first,
            created_at="2026-01-15T10:05:00+00:00",
            correlation_id="transition-002",
        )
        assert second.generation == 2
        assert first.generation == 1  # original is unchanged

    def test_preserves_new_fields(self) -> None:
        first = initial_epoch(
            created_at=_CREATED_AT,
            correlation_id=_CORRELATION_ID,
            label="v1",
        )
        second_ts = "2026-01-15T11:00:00+00:00"
        second = next_epoch(
            first,
            created_at=second_ts,
            correlation_id="transition-002",
            label="v2",
        )
        assert second.generation == 2
        assert second.created_at == second_ts
        assert second.correlation_id == "transition-002"
        assert second.label == "v2"

    def test_accepts_none_label(self) -> None:
        first = initial_epoch(created_at=_CREATED_AT, correlation_id=_CORRELATION_ID)
        second = next_epoch(
            first,
            created_at="2026-01-15T11:00:00+00:00",
            correlation_id="transition-002",
            label=None,
        )
        assert second.label is None
        assert second.generation == 2

    def test_chaining_multiple_epochs(self) -> None:
        e1 = initial_epoch(created_at=_CREATED_AT, correlation_id="corr-1")
        e2 = next_epoch(e1, created_at="2026-01-15T11:00:00+00:00", correlation_id="corr-2")
        e3 = next_epoch(e2, created_at="2026-01-15T12:00:00+00:00", correlation_id="corr-3")
        assert e1.generation == 1
        assert e2.generation == 2
        assert e3.generation == 3

    def test_label_not_propagated_by_default(self) -> None:
        """next_epoch does not propagate label from the current epoch."""
        first = initial_epoch(
            created_at=_CREATED_AT,
            correlation_id=_CORRELATION_ID,
            label="initial",
        )
        second = next_epoch(
            first,
            created_at="2026-01-15T11:00:00+00:00",
            correlation_id="transition-002",
        )
        # label is not passed, so it defaults to None
        assert second.label is None


# --------------------------------------------------------------------------- validation


class TestValidation:
    """Given invalid constructor values, when creating a RuntimeEpoch, then ValueError is raised."""

    def test_rejects_zero_generation(self) -> None:
        with pytest.raises(ValueError, match="positive"):
            RuntimeEpoch(
                generation=EpochGeneration(0),
                created_at=_CREATED_AT,
                correlation_id=_CORRELATION_ID,
                label=None,
            )

    def test_rejects_negative_generation(self) -> None:
        with pytest.raises(ValueError, match="positive"):
            RuntimeEpoch(
                generation=EpochGeneration(-1),
                created_at=_CREATED_AT,
                correlation_id=_CORRELATION_ID,
                label=None,
            )

    def test_rejects_empty_created_at(self) -> None:
        with pytest.raises(ValueError, match="created_at"):
            RuntimeEpoch(
                generation=EpochGeneration(1),
                created_at="",
                correlation_id=_CORRELATION_ID,
                label=None,
            )

    def test_rejects_empty_correlation_id(self) -> None:
        with pytest.raises(ValueError, match="correlation_id"):
            RuntimeEpoch(
                generation=EpochGeneration(1),
                created_at=_CREATED_AT,
                correlation_id="",
                label=None,
            )

    def test_rejects_non_string_label(self) -> None:
        with pytest.raises(ValueError, match="label"):
            RuntimeEpoch(
                generation=EpochGeneration(1),
                created_at=_CREATED_AT,
                correlation_id=_CORRELATION_ID,
                label=42,  # type: ignore[arg-type]
            )


# --------------------------------------------------------------------------- EpochGeneration NewType


class TestEpochGeneration:
    """Given EpochGeneration NewType, it behaves as an int at runtime."""

    def test_is_int_at_runtime(self) -> None:
        epoch = RuntimeEpoch(
            generation=EpochGeneration(5),
            created_at=_CREATED_AT,
            correlation_id=_CORRELATION_ID,
            label=None,
        )
        assert isinstance(epoch.generation, int)
        assert epoch.generation == 5

    def test_construction_with_type(self) -> None:
        gen = EpochGeneration(1)
        assert gen == 1
        assert isinstance(gen, int)


# --------------------------------------------------------------------------- immutability


class TestImmutability:
    """Given a RuntimeEpoch (frozen dataclass), when trying to set an attribute, it is rejected."""

    def test_cannot_set_generation(self) -> None:
        epoch = initial_epoch(created_at=_CREATED_AT, correlation_id=_CORRELATION_ID)
        with pytest.raises(AttributeError):
            epoch.generation = 5  # type: ignore[misc]

    def test_cannot_set_label(self) -> None:
        epoch = initial_epoch(created_at=_CREATED_AT, correlation_id=_CORRELATION_ID)
        with pytest.raises(AttributeError):
            epoch.label = "new-label"  # type: ignore[misc]
