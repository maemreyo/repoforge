"""Tests for #270: generation handoff worker-binding reconciliation."""

from __future__ import annotations

from repoforge.application.activation.handoff import (
    GenerationHandoffReconciler,
    OwnerIdentity,
)
from repoforge.domain.operation_worker import OperationWorkerBinding
from repoforge.ports.process_reaper import ReapOutcome
from repoforge.testing.fakes import InMemoryWorkerBindingStore, RecordingProcessReaper


def _binding(
    op: str,
    *,
    server_pid: int = 100,
    server_start_token: str | None = "srv-A",
    owner_generation: int | None = None,
) -> OperationWorkerBinding:
    return OperationWorkerBinding(
        operation_id=op,
        child_pid=4321,
        child_pgid=4321,
        child_start_token="tok-child",
        server_pid=server_pid,
        server_start_token=server_start_token,
        created_at="2026-07-25T00:00:00+00:00",
        owner_generation=owner_generation,
    )


def _op(suffix: str) -> str:
    # operation ids are op- followed by 24 hex chars.
    return "op-" + suffix.rjust(24, "0")


def test_current_generation_bindings_are_retained_and_others_reaped() -> None:
    store = InMemoryWorkerBindingStore()
    store.put(_binding(_op("a1"), owner_generation=7))  # current
    store.put(_binding(_op("b2"), owner_generation=6))  # prior generation
    reaper = RecordingProcessReaper()
    reconciler = GenerationHandoffReconciler(bindings=store, reaper=reaper)

    report = reconciler.reconcile(
        current_owner=OwnerIdentity(server_pid=100, server_start_token="srv-A", generation=7)
    )

    assert report.retained == (_op("a1"),)
    assert report.reaped == (_op("b2"),)
    assert store.get(_op("a1")) is not None
    assert store.get(_op("b2")) is None
    assert [b.operation_id for b in reaper.reaped] == [_op("b2")]


def test_pre_v2_bindings_are_attributed_by_server_process_identity() -> None:
    # owner_generation is None (pre-v2). The binding whose server identity matches the
    # current process is retained; the one from a dead prior server is reconciled.
    store = InMemoryWorkerBindingStore()
    store.put(_binding(_op("a1"), server_pid=100, server_start_token="srv-A"))
    store.put(_binding(_op("b2"), server_pid=200, server_start_token="srv-OLD"))
    reaper = RecordingProcessReaper()
    reconciler = GenerationHandoffReconciler(bindings=store, reaper=reaper)

    report = reconciler.reconcile(
        current_owner=OwnerIdentity(server_pid=100, server_start_token="srv-A")
    )

    assert report.retained == (_op("a1"),)
    assert report.reaped == (_op("b2"),)


def test_recycled_pid_with_a_different_start_token_is_not_adopted() -> None:
    # Same server_pid but a different start token means a different process: the old
    # binding must be reconciled, never adopted as the current generation's.
    store = InMemoryWorkerBindingStore()
    store.put(_binding(_op("a1"), server_pid=100, server_start_token="srv-OLD"))
    reaper = RecordingProcessReaper()
    reconciler = GenerationHandoffReconciler(bindings=store, reaper=reaper)

    report = reconciler.reconcile(
        current_owner=OwnerIdentity(server_pid=100, server_start_token="srv-NEW")
    )

    assert report.retained == ()
    assert report.reaped == (_op("a1"),)


def test_resumable_operations_are_kept_across_the_handoff() -> None:
    store = InMemoryWorkerBindingStore()
    store.put(_binding(_op("a1"), owner_generation=6))  # prior gen, resumable
    store.put(_binding(_op("b2"), owner_generation=6))  # prior gen, not resumable
    reaper = RecordingProcessReaper()
    reconciler = GenerationHandoffReconciler(bindings=store, reaper=reaper)

    report = reconciler.reconcile(
        current_owner=OwnerIdentity(server_pid=100, server_start_token="srv-A", generation=7),
        is_resumable=lambda op: op == _op("a1"),
    )

    assert report.resumable_kept == (_op("a1"),)
    assert report.reaped == (_op("b2"),)
    # The resumable binding is left in place for the new generation to adopt.
    assert store.get(_op("a1")) is not None
    assert reaper.reaped == [b for b in reaper.reaped if b.operation_id == _op("b2")]


def test_a_surviving_worker_fails_closed_and_keeps_its_binding() -> None:
    """If the old worker survived termination, ownership must NOT be released."""
    store = InMemoryWorkerBindingStore()
    store.put(_binding(_op("b2"), owner_generation=6))
    reaper = RecordingProcessReaper(
        outcome=ReapOutcome(
            attempted=True, reaped=False, still_alive=True, detail="survived SIGKILL"
        )
    )
    reconciler = GenerationHandoffReconciler(bindings=store, reaper=reaper)

    report = reconciler.reconcile(
        current_owner=OwnerIdentity(server_pid=100, server_start_token="srv-A", generation=7)
    )

    assert report.ok is False
    assert report.conflicts and report.conflicts[0][0] == _op("b2")
    # The binding is retained so the new generation cannot claim the live work.
    assert store.get(_op("b2")) is not None
    assert report.released == ()
    assert report.reaped == ()


def test_a_binding_replaced_during_reconciliation_is_not_deleted() -> None:
    """A newer generation's binding written mid-scan must never be released."""
    store = InMemoryWorkerBindingStore()
    stale = _binding(_op("b2"), owner_generation=6)
    store.put(stale)

    class _RacingStore:
        """Returns the stale record from list_all, but a replaced one from get."""

        def __init__(self, inner: InMemoryWorkerBindingStore) -> None:
            self._inner = inner

        def list_all(self, *, max_records: int = 2_000):
            return (stale,)

        def get(self, operation_id: str):
            return self._inner.get(operation_id)

        def put(self, binding) -> None:
            self._inner.put(binding)

        def delete(self, operation_id: str) -> None:
            self._inner.delete(operation_id)

        def delete_if_unchanged(self, binding) -> bool:
            return self._inner.delete_if_unchanged(binding)

    # Simulate the replacement: same operation, new owner generation.
    store.put(_binding(_op("b2"), owner_generation=8))
    reconciler = GenerationHandoffReconciler(
        bindings=_RacingStore(store), reaper=RecordingProcessReaper()
    )

    report = reconciler.reconcile(
        current_owner=OwnerIdentity(server_pid=100, server_start_token="srv-A", generation=7)
    )

    assert report.ok is False
    assert "replaced" in report.conflicts[0][1]
    assert store.get(_op("b2")) is not None


def test_a_dead_child_is_released_not_reaped() -> None:
    # When the reaper reports nothing was signalled (child already gone), the binding
    # is still released but classified as released rather than reaped.
    store = InMemoryWorkerBindingStore()
    store.put(_binding(_op("b2"), owner_generation=6))
    reaper = RecordingProcessReaper(
        outcome=ReapOutcome(attempted=True, reaped=False, still_alive=False, detail="already gone")
    )
    reconciler = GenerationHandoffReconciler(bindings=store, reaper=reaper)

    report = reconciler.reconcile(
        current_owner=OwnerIdentity(server_pid=100, server_start_token="srv-A", generation=7)
    )

    assert report.released == (_op("b2"),)
    assert report.reaped == ()
    assert store.get(_op("b2")) is None
