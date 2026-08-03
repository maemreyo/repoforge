"""Effect-owning runtime transition coordinator (F-009).

The activation ledger records every activation/switch/rollback attempt durably
BEFORE its pointer mutation, so a crash leaves a trace reconciliation can act
on. This coordinator owns that ledger:

* ``record_attempt`` writes exactly one PREPARED record -- it never fabricates
  CONFIG_GENERATED/DEPENDENCIES_RESOLVED/VALIDATED/STAGED/ACTIVATED phases that
  no effect produced;
* ``mark_effect`` advances the record by exactly one real phase, CAS-bound to
  the caller's revision, after the corresponding effect has happened;
* ``mark_outcome`` terminalizes through an exhaustive outcome set -- anything
  else raises instead of silently degrading to success (fail-open is a bug);
* ``reconcile_active`` lists every non-terminal transition so a recovery pass
  can complete whichever durable tail (journal, receipt, terminal transition)
  is missing after a crash.

A transition is terminal only through COMPLETED / ROLLED_BACK or an explicit
terminal failure; ``is_terminal_failure`` keeps a persisted failure distinct
from the retryable in-memory statuses. Retrying an activation creates a NEW
transition rather than resuming a failed one.
"""

from __future__ import annotations

from collections.abc import Callable

from ...domain.durable_state import Revision, StateEnvelope
from ...domain.errors import ConfigError
from ...domain.runtime_transition import (
    RuntimeTransition,
    RuntimeTransitionStatus,
    is_terminal,
    is_terminal_failure,
    new_runtime_transition,
)
from ...ports.clock import Clock
from ...ports.ids import IdGenerator
from ...ports.runtime_transition_store import RuntimeTransitionStore

_S = RuntimeTransitionStatus

#: Linear forward chain the domain's state machine walks. The coordinator moves a
#: record along this chain in memory to reach a real effect boundary, but persists
#: only the boundary status -- the intermediate statuses (CONFIG_GENERATED etc.)
#: name effects that never happened, so no record of them is ever written.
_FORWARD_CHAIN: tuple[RuntimeTransitionStatus, ...] = (
    _S.PREPARED,
    _S.CONFIG_GENERATED,
    _S.DEPENDENCIES_RESOLVED,
    _S.VALIDATED,
    _S.STAGED,
    _S.ACTIVATED,
    _S.HEALTH_CHECKED,
    _S.READY,
    _S.COMPLETED,
)

#: Statuses a coordinator may persist as a terminal outcome. Any other status is
#: refused: an unsupported outcome must raise, never silently become success.
_TERMINAL_OUTCOMES = frozenset(
    {
        _S.COMPLETED,
        _S.ROLLED_BACK,
        _S.CONFIG_FAILED,
        _S.VALIDATION_FAILED,
        _S.ACTIVATION_FAILED,
        _S.HEALTH_FAILED,
    }
)

#: Real effect boundaries the coordinator knows. Each may only be written after
#: the effect it names actually happened. Terminal outcomes are excluded: they go
#: through ``mark_outcome``, never through ``mark_effect``.
_REAL_PHASES = frozenset(
    {
        _S.CONFIG_GENERATED,
        _S.DEPENDENCIES_RESOLVED,
        _S.VALIDATED,
        _S.STAGED,
        _S.ACTIVATED,
        _S.HEALTH_CHECKED,
        _S.READY,
    }
)

#: The forward-chain status each terminal outcome legally leaves from. The
#: coordinator walks to this branch point in memory, then applies the outcome.
_OUTCOME_BRANCH: dict[RuntimeTransitionStatus, RuntimeTransitionStatus] = {
    _S.COMPLETED: _S.READY,
    _S.ROLLED_BACK: _S.HEALTH_CHECKED,
    _S.CONFIG_FAILED: _S.PREPARED,
    _S.VALIDATION_FAILED: _S.VALIDATED,
    _S.ACTIVATION_FAILED: _S.STAGED,
    _S.HEALTH_FAILED: _S.HEALTH_CHECKED,
}

#: The domain method each terminal outcome applies at its branch point.
_OUTCOME_APPLY: dict[RuntimeTransitionStatus, Callable[..., RuntimeTransition]] = {
    _S.COMPLETED: lambda transition, *, now, error_code, error_message: transition.complete(
        updated_at=now
    ),
    _S.ROLLED_BACK: lambda transition, *, now, error_code, error_message: transition.rollback(
        updated_at=now
    ),
    _S.CONFIG_FAILED: lambda transition, *, now, error_code, error_message: transition.fail_config(
        updated_at=now,
        error_code=error_code or "CONFIG_FAILED",
        error_message=error_message or "configuration generation failed",
    ),
    _S.VALIDATION_FAILED: lambda transition, *, now, error_code, error_message: (
        transition.fail_validation(
            updated_at=now,
            error_code=error_code or "VALIDATION_FAILED",
            error_message=error_message or "validation failed",
        )
    ),
    _S.ACTIVATION_FAILED: lambda transition, *, now, error_code, error_message: (
        transition.fail_activation(
            updated_at=now,
            error_code=error_code or "ACTIVATION_FAILED",
            error_message=error_message or "activation did not converge",
        )
    ),
    _S.HEALTH_FAILED: lambda transition, *, now, error_code, error_message: transition.fail_health(
        updated_at=now,
        error_code=error_code or "HEALTH_FAILED",
        error_message=error_message or "health verification failed",
    ),
}


class RuntimeTransitionCoordinator:
    """Own the runtime-transition ledger; never a passive side ledger."""

    def __init__(
        self,
        *,
        transitions: RuntimeTransitionStore,
        ids: IdGenerator,
        clock: Clock,
    ) -> None:
        self._transitions = transitions
        self._ids = ids
        self._clock = clock

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
    ) -> StateEnvelope[RuntimeTransition]:
        """Durably record the attempt as PREPARED before any effect.

        Exactly one record, in PREPARED state: no phases are pre-written. If this
        write fails, the caller has journaled nothing, so no dead journal is left
        behind (ledger-create failure must never strand a recovery cursor).
        """
        now = self._clock.now_iso()
        return self._transitions.create(
            new_runtime_transition(
                transition_id=f"tran-{self._ids.new_hex(24)}",
                target_generation=target_generation,
                correlation_id=correlation_id,
                started_at=now,
                config_generation=config_generation,
                previous_transition_id=previous_transition_id,
                kind=kind,
                from_sha=from_sha,
                to_sha=to_sha,
            )
        )

    def mark_effect(
        self,
        transition: RuntimeTransition,
        *,
        status: RuntimeTransitionStatus,
        expected_revision: Revision,
    ) -> StateEnvelope[RuntimeTransition]:
        """Advance to a real effect boundary, after the effect actually happened.

        The status must be a real phase (never a jump straight to a terminal
        outcome -- terminalization goes through ``mark_outcome``). The domain
        state machine is walked in memory from the record's current status to the
        boundary, but only the boundary status is persisted: the intermediate
        statuses name effects that never happened, so writing them would fabricate
        history. CAS-bound to the caller's revision so a stale writer cannot
        advance a record it no longer owns.
        """
        if status not in _REAL_PHASES:
            raise ConfigError(
                f"RUNTIME_TRANSITION_INVALID_PHASE: {status.value} is not a real effect boundary"
            )
        current_index = _index_in_chain(transition.status)
        target_index = _index_in_chain(status)
        if target_index < current_index:
            raise ConfigError(
                "RUNTIME_TRANSITION_ILLEGAL: "
                f"cannot regress {transition.status.value} -> {status.value}"
            )
        now = self._clock.now_iso()
        walked = transition
        for phase in _FORWARD_CHAIN[current_index + 1 : target_index + 1]:
            walked = _apply_domain(walked, phase, now=now)
        return self._transitions.save(walked, expected_revision=expected_revision)

    def mark_outcome(
        self,
        transition: RuntimeTransition,
        *,
        outcome: RuntimeTransitionStatus,
        expected_revision: Revision,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> StateEnvelope[RuntimeTransition]:
        """Terminalize through an exhaustive outcome set; anything else raises.

        Unsupported outcomes fail loudly instead of degrading to success: a
        requested ``health_failed`` that a caller misspelled must not persist as
        ``completed``. The domain's forward chain is walked in memory to the
        branch point each terminal outcome legally leaves from -- the persisted
        record is only the terminal state, never fabricated intermediate phases.
        """
        if outcome not in _TERMINAL_OUTCOMES:
            raise ConfigError(
                "RUNTIME_TRANSITION_UNSUPPORTED_OUTCOME: "
                f"{outcome.value} is not a supported terminal outcome"
            )
        now = self._clock.now_iso()
        try:
            terminal = self._terminalize(
                transition,
                outcome=outcome,
                now=now,
                error_code=error_code,
                error_message=error_message,
            )
        except Exception as exc:
            raise ConfigError(
                "RUNTIME_TRANSITION_ILLEGAL: "
                f"cannot terminalize {transition.status.value} -> {outcome.value}: {exc}"
            ) from exc
        return self._transitions.save(terminal, expected_revision=expected_revision)

    def reconcile_active(
        self, *, max_records: int = 100
    ) -> tuple[StateEnvelope[RuntimeTransition], ...]:
        """Every non-terminal transition, for recovery of the durable tail.

        A persisted terminal failure is terminal: an ACTIVATION_FAILED record is a
        completed outcome (a retry is a new transition), so it must never be listed
        here or recovery would loop on a record nothing can advance (F-009).
        """
        page = self._transitions.list_all(max_records=max_records)
        return tuple(
            item
            for item in page.records
            if not is_terminal(item.value.status) and not is_terminal_failure(item.value.status)
        )

    def get_active_by_correlation(
        self, correlation_id: str
    ) -> StateEnvelope[RuntimeTransition] | None:
        return self._transitions.get_active_by_correlation(correlation_id)

    def read(self, transition_id: str) -> StateEnvelope[RuntimeTransition] | None:
        return self._transitions.read(transition_id)

    @staticmethod
    def _terminalize(
        transition: RuntimeTransition,
        *,
        outcome: RuntimeTransitionStatus,
        now: str,
        error_code: str | None,
        error_message: str | None,
    ) -> RuntimeTransition:
        """Build the terminal record, walking the forward chain to its branch point."""
        branch = _OUTCOME_BRANCH[outcome]
        current = _index_in_chain(transition.status)
        target = _index_in_chain(branch)
        walked = transition
        for phase in _FORWARD_CHAIN[current + 1 : target + 1]:
            walked = _apply_domain(walked, phase, now=now)
        return _OUTCOME_APPLY[outcome](
            walked,
            now=now,
            error_code=error_code,
            error_message=error_message,
        )


__all__ = ["RuntimeTransitionCoordinator"]


def _index_in_chain(status: RuntimeTransitionStatus) -> int:
    try:
        return _FORWARD_CHAIN.index(status)
    except ValueError:
        raise ConfigError(
            f"RUNTIME_TRANSITION_ILLEGAL: {status.value} is not on the forward chain"
        ) from None


def _apply_domain(
    transition: RuntimeTransition,
    phase: RuntimeTransitionStatus,
    *,
    now: str,
) -> RuntimeTransition:
    """Apply one domain transition step; a skipped step raises."""
    try:
        return transition._advance(phase, updated_at=now)
    except Exception as exc:
        raise ConfigError(
            "RUNTIME_TRANSITION_ILLEGAL: "
            f"cannot advance {transition.status.value} -> {phase.value}: {exc}"
        ) from exc
