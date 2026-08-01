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
