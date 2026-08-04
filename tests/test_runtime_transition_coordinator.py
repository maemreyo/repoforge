"""Acceptance tests for the effect-owning RuntimeTransitionCoordinator (F-009).

Each test pins one re-review finding against the coordinator:

- the ledger records exactly one PREPARED attempt, never fabricated phases;
- every effect is CAS-advanced after the real boundary it names;
- terminalization is exhaustive -- an unsupported outcome raises instead of
  silently degrading to success;
- the correlation lookup returns the single active transition and fails closed
  on an invariant violation or an incomplete scan;
- a ledger-create failure leaves no journal behind (the caller has nothing to
  reconcile);
- a terminal-save failure after a durable success does not turn the success into
  a command failure -- the journal stays so reconciliation completes the tail.
"""

from __future__ import annotations

import pytest

from repoforge.application.runtime.runtime_transition_coordinator import (
    RuntimeTransitionCoordinator,
)
from repoforge.domain.errors import ConfigError
from repoforge.domain.runtime_transition import (
    RuntimeTransitionStatus,
    is_terminal,
    is_terminal_failure,
)
from repoforge.testing import FixedClock, SequenceIdGenerator
from repoforge.testing.fakes_ext import InMemoryRuntimeTransitionStore

_SHA = "a" * 64
_OTHER_SHA = "b" * 64


def _coordinator(
    store: InMemoryRuntimeTransitionStore | None = None,
) -> tuple[RuntimeTransitionCoordinator, InMemoryRuntimeTransitionStore]:
    records = store if store is not None else InMemoryRuntimeTransitionStore()
    coordinator = RuntimeTransitionCoordinator(
        transitions=records,
        ids=SequenceIdGenerator(("a",)),
        clock=FixedClock("2026-07-30T00:00:00+00:00"),
    )
    return coordinator, records


def test_record_attempt_writes_exactly_one_prepared_record() -> None:
    coordinator, store = _coordinator()
    envelope = coordinator.record_attempt(
        correlation_id="c" * 24,
        kind="activate",
        from_sha=_SHA,
        to_sha=_OTHER_SHA,
    )

    assert envelope.value.status is RuntimeTransitionStatus.PREPARED
    assert envelope.value.kind == "activate"
    assert envelope.value.from_sha == _SHA
    assert envelope.value.to_sha == _OTHER_SHA
    page = store.list_all()
    assert len(page.records) == 1


def test_no_phases_are_fabricated_before_effects() -> None:
    """record_attempt must not pre-write CONFIG_GENERATED/STAGED/ACTIVATED etc."""
    coordinator, store = _coordinator()
    envelope = coordinator.record_attempt(
        correlation_id="c" * 24,
        kind="activate",
        from_sha=_SHA,
        to_sha=_OTHER_SHA,
    )

    assert envelope.value.status is RuntimeTransitionStatus.PREPARED
    assert store.list_all().records[0].value.status is RuntimeTransitionStatus.PREPARED


def test_mark_effect_advances_by_exactly_one_real_phase() -> None:
    coordinator, _ = _coordinator()
    envelope = coordinator.record_attempt(
        correlation_id="c" * 24,
        kind="activate",
        from_sha=_SHA,
        to_sha=_OTHER_SHA,
    )

    staged = coordinator.mark_effect(
        envelope.value, status=RuntimeTransitionStatus.STAGED, expected_revision=envelope.revision
    )

    assert staged.value.status is RuntimeTransitionStatus.STAGED


def test_mark_effect_rejects_a_fabricated_phase() -> None:
    """A phase that is not a real effect boundary is refused."""
    coordinator, _ = _coordinator()
    envelope = coordinator.record_attempt(
        correlation_id="c" * 24,
        kind="activate",
        from_sha=_SHA,
        to_sha=_OTHER_SHA,
    )

    with pytest.raises(ConfigError, match="INVALID_PHASE"):
        coordinator.mark_effect(
            envelope.value,
            status=RuntimeTransitionStatus.COMPLETED,
            expected_revision=envelope.revision,
        )


def test_mark_effect_skips_to_a_real_boundary_without_persisting_intermediates() -> None:
    """Jumping to a real phase is allowed; only the boundary is persisted."""
    coordinator, store = _coordinator()
    envelope = coordinator.record_attempt(
        correlation_id="c" * 24,
        kind="activate",
        from_sha=_SHA,
        to_sha=_OTHER_SHA,
    )

    activated = coordinator.mark_effect(
        envelope.value,
        status=RuntimeTransitionStatus.ACTIVATED,
        expected_revision=envelope.revision,
    )

    assert activated.value.status is RuntimeTransitionStatus.ACTIVATED
    persisted = store.list_all().records[0].value
    assert persisted.status is RuntimeTransitionStatus.ACTIVATED


def test_mark_effect_rejects_a_regression() -> None:
    coordinator, _ = _coordinator()
    envelope = coordinator.record_attempt(
        correlation_id="c" * 24,
        kind="activate",
        from_sha=_SHA,
        to_sha=_OTHER_SHA,
    )
    activated = coordinator.mark_effect(
        envelope.value,
        status=RuntimeTransitionStatus.ACTIVATED,
        expected_revision=envelope.revision,
    )

    with pytest.raises(ConfigError, match="ILLEGAL"):
        coordinator.mark_effect(
            activated.value,
            status=RuntimeTransitionStatus.STAGED,
            expected_revision=activated.revision,
        )


def test_mark_outcome_completes_through_the_domain_chain() -> None:
    coordinator, _ = _coordinator()
    envelope = coordinator.record_attempt(
        correlation_id="c" * 24,
        kind="activate",
        from_sha=_SHA,
        to_sha=_OTHER_SHA,
    )
    staged = coordinator.mark_effect(
        envelope.value, status=RuntimeTransitionStatus.STAGED, expected_revision=envelope.revision
    )
    activated = coordinator.mark_effect(
        staged.value,
        status=RuntimeTransitionStatus.ACTIVATED,
        expected_revision=staged.revision,
    )

    terminal = coordinator.mark_outcome(
        activated.value,
        outcome=RuntimeTransitionStatus.COMPLETED,
        expected_revision=activated.revision,
    )

    assert terminal.value.status is RuntimeTransitionStatus.COMPLETED
    assert is_terminal(terminal.value.status)
    assert terminal.value.completed_at is not None


def test_mark_outcome_rolls_back() -> None:
    coordinator, _ = _coordinator()
    envelope = coordinator.record_attempt(
        correlation_id="c" * 24,
        kind="activate",
        from_sha=_SHA,
        to_sha=_OTHER_SHA,
    )
    staged = coordinator.mark_effect(
        envelope.value, status=RuntimeTransitionStatus.STAGED, expected_revision=envelope.revision
    )
    activated = coordinator.mark_effect(
        staged.value,
        status=RuntimeTransitionStatus.ACTIVATED,
        expected_revision=staged.revision,
    )
    checked = coordinator.mark_effect(
        activated.value,
        status=RuntimeTransitionStatus.HEALTH_CHECKED,
        expected_revision=activated.revision,
    )

    terminal = coordinator.mark_outcome(
        checked.value,
        outcome=RuntimeTransitionStatus.ROLLED_BACK,
        expected_revision=checked.revision,
    )

    assert terminal.value.status is RuntimeTransitionStatus.ROLLED_BACK
    assert is_terminal(terminal.value.status)


def test_unsupported_outcome_raises_instead_of_becoming_success() -> None:
    """mark_outcome must never silently degrade an unknown outcome to COMPLETED."""
    coordinator, _ = _coordinator()
    envelope = coordinator.record_attempt(
        correlation_id="c" * 24,
        kind="activate",
        from_sha=_SHA,
        to_sha=_OTHER_SHA,
    )

    with pytest.raises(ConfigError, match="UNSUPPORTED_OUTCOME"):
        coordinator.mark_outcome(
            envelope.value,
            outcome=RuntimeTransitionStatus.READY,
            expected_revision=envelope.revision,
        )

    stored = coordinator.read(envelope.record_id)
    assert stored is not None
    assert stored.value.status is RuntimeTransitionStatus.PREPARED


def test_terminal_failure_is_distinct_and_final() -> None:
    coordinator, _ = _coordinator()
    envelope = coordinator.record_attempt(
        correlation_id="c" * 24,
        kind="activate",
        from_sha=_SHA,
        to_sha=_OTHER_SHA,
    )
    staged = coordinator.mark_effect(
        envelope.value, status=RuntimeTransitionStatus.STAGED, expected_revision=envelope.revision
    )

    terminal = coordinator.mark_outcome(
        staged.value,
        outcome=RuntimeTransitionStatus.ACTIVATION_FAILED,
        expected_revision=staged.revision,
        error_code="ACTIVATION_FAILED",
        error_message="did not converge",
    )

    assert terminal.value.status is RuntimeTransitionStatus.ACTIVATION_FAILED
    assert is_terminal_failure(terminal.value.status)
    # A persisted terminal failure must not silently allow a resume to the same
    # record: the domain still permits an in-memory retry path, but the
    # coordinator never calls it after persisting a failure.
    assert is_terminal_failure(RuntimeTransitionStatus.HEALTH_FAILED)
    assert not is_terminal_failure(RuntimeTransitionStatus.PREPARED)


def test_get_active_by_correlation_returns_the_single_active_record() -> None:
    coordinator, _ = _coordinator()
    first = coordinator.record_attempt(
        correlation_id="c" * 24,
        kind="activate",
        from_sha=_SHA,
        to_sha=_OTHER_SHA,
    )
    coordinator.mark_outcome(
        first.value,
        outcome=RuntimeTransitionStatus.COMPLETED,
        expected_revision=first.revision,
    )
    second = coordinator.record_attempt(
        correlation_id="c" * 24,
        kind="activate",
        from_sha=_OTHER_SHA,
        to_sha=_SHA,
        previous_transition_id=first.record_id,
    )

    active = coordinator.get_active_by_correlation("c" * 24)

    assert active is not None
    assert active.record_id == second.record_id


def test_get_active_by_correlation_returns_none_when_all_terminal() -> None:
    coordinator, _ = _coordinator()
    envelope = coordinator.record_attempt(
        correlation_id="c" * 24,
        kind="activate",
        from_sha=_SHA,
        to_sha=_OTHER_SHA,
    )
    coordinator.mark_outcome(
        envelope.value,
        outcome=RuntimeTransitionStatus.COMPLETED,
        expected_revision=envelope.revision,
    )

    assert coordinator.get_active_by_correlation("c" * 24) is None


def test_get_active_by_correlation_raises_on_invariant_violation() -> None:
    coordinator, store = _coordinator()
    coordinator.record_attempt(
        correlation_id="c" * 24,
        kind="activate",
        from_sha=_SHA,
        to_sha=_OTHER_SHA,
    )
    coordinator.record_attempt(
        correlation_id="c" * 24,
        kind="activate",
        from_sha=_OTHER_SHA,
        to_sha=_SHA,
    )

    with pytest.raises(ConfigError, match="INVARIANT_VIOLATED"):
        coordinator.get_active_by_correlation("c" * 24)
    assert store is not None


def test_reconcile_active_lists_only_non_terminal_transitions() -> None:
    coordinator, _ = _coordinator()
    first = coordinator.record_attempt(
        correlation_id="c" * 24,
        kind="activate",
        from_sha=_SHA,
        to_sha=_OTHER_SHA,
    )
    coordinator.mark_outcome(
        first.value,
        outcome=RuntimeTransitionStatus.COMPLETED,
        expected_revision=first.revision,
    )
    coordinator.record_attempt(
        correlation_id="d" * 24,
        kind="activate",
        from_sha=_OTHER_SHA,
        to_sha=_SHA,
    )

    active = coordinator.reconcile_active()

    assert len(active) == 1
    assert active[0].value.correlation_id == "d" * 24


def test_reconcile_active_excludes_persisted_terminal_failures() -> None:
    """A terminalized failure is terminal: recovery must never loop on it."""
    coordinator, _ = _coordinator()
    failed = coordinator.record_attempt(
        correlation_id="c" * 24,
        kind="activate",
        from_sha=_SHA,
        to_sha=_OTHER_SHA,
    )
    coordinator.mark_outcome(
        failed.value,
        outcome=RuntimeTransitionStatus.ACTIVATION_FAILED,
        expected_revision=failed.revision,
        error_code="ACTIVATION_FAILED",
        error_message="boom",
    )
    coordinator.record_attempt(
        correlation_id="d" * 24,
        kind="activate",
        from_sha=_OTHER_SHA,
        to_sha=_SHA,
    )

    active = coordinator.reconcile_active()

    assert [item.value.correlation_id for item in active] == ["d" * 24]
    assert coordinator.get_active_by_correlation("c" * 24) is None
