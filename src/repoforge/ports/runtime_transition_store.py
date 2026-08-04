"""Persistence boundary for durable runtime-transition state."""

from __future__ import annotations

from typing import Protocol

from ..domain.durable_state import Revision, StateEnvelope, StatePage
from ..domain.runtime_transition import RuntimeTransition


class RuntimeTransitionStore(Protocol):
    def create(self, transition: RuntimeTransition) -> StateEnvelope[RuntimeTransition]: ...

    def read(self, transition_id: str) -> StateEnvelope[RuntimeTransition] | None: ...

    def save(
        self,
        transition: RuntimeTransition,
        *,
        expected_revision: Revision,
    ) -> StateEnvelope[RuntimeTransition]: ...

    def list_all(self, *, max_records: int = 2_000) -> StatePage[RuntimeTransition]: ...

    def list_by_generation(
        self,
        generation: int,
        *,
        max_records: int = 100,
    ) -> StatePage[RuntimeTransition]: ...

    def get_active_by_correlation(
        self, correlation_id: str, *, max_records: int = 100
    ) -> StateEnvelope[RuntimeTransition] | None:
        """The single non-terminal transition for one lifecycle correlation.

        Invariant: at most one non-terminal transition may exist per correlation
        id -- the coordinator never opens a second attempt while one is in flight.
        Returns ``None`` when every matching transition is terminal; raises a
        typed error when the store holds more than one non-terminal match (the
        invariant was violated) or when the scan was truncated, because either
        makes the "the active one" answer unprovable.
        """
        ...
