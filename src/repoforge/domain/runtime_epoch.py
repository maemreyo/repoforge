"""Monotonic runtime epoch for tracking runtime configuration generations.

Each RepoForge runtime starts at ``generation=1`` and advances by 1 on every
successful generation transition. An epoch carries the wall-clock timestamp
of its creation, the correlation id of the transition that produced it, and
an optional human-readable label describing the generation's purpose.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import NewType

EpochGeneration = NewType("EpochGeneration", int)


@dataclass(frozen=True, slots=True)
class RuntimeEpoch:
    """Monotonic counter for a single runtime's configuration generations."""

    generation: EpochGeneration
    created_at: str
    correlation_id: str
    label: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.generation, int) or isinstance(self.generation, bool):
            raise ValueError("generation must be an int")
        if self.generation <= 0:
            raise ValueError("generation must be positive")
        if not isinstance(self.created_at, str) or not self.created_at:
            raise ValueError("created_at must be a non-empty string")
        if not isinstance(self.correlation_id, str) or not self.correlation_id:
            raise ValueError("correlation_id must be a non-empty string")
        if self.label is not None and not isinstance(self.label, str):
            raise ValueError("label must be a string or None")


def initial_epoch(
    *,
    created_at: str,
    correlation_id: str,
    label: str | None = None,
) -> RuntimeEpoch:
    """Return the first epoch (generation=1) for a fresh runtime."""
    return RuntimeEpoch(
        generation=EpochGeneration(1),
        created_at=created_at,
        correlation_id=correlation_id,
        label=label,
    )


def next_epoch(
    current: RuntimeEpoch,
    *,
    created_at: str,
    correlation_id: str,
    label: str | None = None,
) -> RuntimeEpoch:
    """Return the successor epoch with ``generation = current.generation + 1``."""
    return RuntimeEpoch(
        generation=EpochGeneration(current.generation + 1),
        created_at=created_at,
        correlation_id=correlation_id,
        label=label,
    )


__all__ = ["EpochGeneration", "RuntimeEpoch", "initial_epoch", "next_epoch"]
