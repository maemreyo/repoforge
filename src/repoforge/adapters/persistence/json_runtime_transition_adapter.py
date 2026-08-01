"""Deferred — Phase 3 JSON persistence for RuntimeTransitionStore."""

from __future__ import annotations

from ...domain.durable_state import Revision, StateEnvelope, StatePage
from ...domain.runtime_transition import RuntimeTransition
from ...ports.runtime_transition_store import RuntimeTransitionStore


class JsonRuntimeTransitionAdapter(RuntimeTransitionStore):
    """JSON-backed RuntimeTransitionStore — deferred to Phase 3."""

    def create(self, transition: RuntimeTransition) -> StateEnvelope[RuntimeTransition]:
        raise NotImplementedError("JsonRuntimeTransitionAdapter deferred to Phase 3")

    def read(self, transition_id: str) -> StateEnvelope[RuntimeTransition] | None:
        raise NotImplementedError("JsonRuntimeTransitionAdapter deferred to Phase 3")

    def save(
        self,
        transition: RuntimeTransition,
        *,
        expected_revision: Revision,
    ) -> StateEnvelope[RuntimeTransition]:
        raise NotImplementedError("JsonRuntimeTransitionAdapter deferred to Phase 3")

    def list_all(self, *, max_records: int = 2_000) -> StatePage[RuntimeTransition]:
        raise NotImplementedError("JsonRuntimeTransitionAdapter deferred to Phase 3")

    def list_by_generation(
        self,
        generation: int,
        *,
        max_records: int = 100,
    ) -> StatePage[RuntimeTransition]:
        raise NotImplementedError("JsonRuntimeTransitionAdapter deferred to Phase 3")
