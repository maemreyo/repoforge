"""Effect-owning runtime-transition coordinator boundary (F-009).

The application layer depends on this narrow protocol, not on the concrete
coordinator, so the upgrade service and bootstrap stay decoupled.
"""

from __future__ import annotations

from typing import Protocol

from ..domain.durable_state import Revision, StateEnvelope
from ..domain.runtime_transition import RuntimeTransition, RuntimeTransitionStatus


class RuntimeTransitionCoordinator(Protocol):
    """Own the runtime-transition ledger: record, effect, terminalize, recover."""

    def record_attempt(
        self,
        *,
        correlation_id: str,
        target_generation: int | None = None,
        config_generation: int | None = None,
        previous_transition_id: str | None = None,
        kind: str = "activate",
        from_sha: str | None = None,
        to_sha: str | None = None,
    ) -> StateEnvelope[RuntimeTransition]: ...

    def read(self, transition_id: str) -> StateEnvelope[RuntimeTransition] | None: ...

    def mark_effect(
        self,
        transition: RuntimeTransition,
        *,
        status: RuntimeTransitionStatus,
        expected_revision: Revision,
    ) -> StateEnvelope[RuntimeTransition]: ...

    def mark_outcome(
        self,
        transition: RuntimeTransition,
        *,
        outcome: RuntimeTransitionStatus,
        expected_revision: Revision,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> StateEnvelope[RuntimeTransition]: ...

    def reconcile_active(
        self, *, max_records: int = 100
    ) -> tuple[StateEnvelope[RuntimeTransition], ...]: ...

    def get_active_by_correlation(
        self, correlation_id: str
    ) -> StateEnvelope[RuntimeTransition] | None: ...
