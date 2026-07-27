"""Generation handoff: reconcile worker bindings so two generations never double-run.

When a new runtime generation takes over from an old (draining) one, both could hold
bindings pointing at the *same* background operation. The stores serialize writes and
fail the loser with a retryable stale error, but nothing stops both from *attempting*
the work -- and background operations are not guaranteed idempotent. The worker binding
(#256) is the ownership token that resolves this: the incoming generation reaps and
releases every binding owned by a prior generation before it claims new work.

A binding belongs to the current generation when its ``owner_generation`` matches (the
schema-v2 token) or, for pre-v2 bindings that predate the token, when its owning server
process identity (``server_pid`` + ``server_start_token``) matches the current process.
Everything else is a prior generation's and is reconciled: its detached child is reaped
(unless the operation kind is resumable across a handoff) and its binding released.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from ...domain.operation_worker import OperationWorkerBinding
from ...ports.process_reaper import ProcessReaper
from ...ports.worker_binding_store import WorkerBindingStore


@dataclass(frozen=True, slots=True)
class OwnerIdentity:
    """The identity of the runtime generation performing the reconciliation."""

    server_pid: int
    server_start_token: str | None
    generation: int | None = None


@dataclass(frozen=True, slots=True)
class HandoffReport:
    """What the reconciliation did, for the activation receipt and audit.

    ``conflicts`` is the fail-closed signal: a prior generation's worker survived
    termination (or its binding was replaced mid-scan), so ownership could not be
    transferred. ``ok`` is False whenever any conflict was recorded, and the caller
    must abort the handoff rather than let a second generation claim that work.
    """

    scanned: int
    retained: tuple[str, ...] = ()
    reaped: tuple[str, ...] = ()
    released: tuple[str, ...] = ()
    resumable_kept: tuple[str, ...] = ()
    conflicts: tuple[tuple[str, str], ...] = ()

    @property
    def ok(self) -> bool:
        return not self.conflicts

    def as_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "scanned": self.scanned,
            "retained": list(self.retained),
            "reaped": list(self.reaped),
            "released": list(self.released),
            "resumable_kept": list(self.resumable_kept),
            "conflicts": [{"operation_id": op, "reason": reason} for op, reason in self.conflicts],
        }


class GenerationHandoffReconciler:
    """Release/reap worker bindings not owned by the current generation."""

    def __init__(
        self, *, bindings: WorkerBindingStore, reaper: ProcessReaper, max_records: int = 2_000
    ) -> None:
        self._bindings = bindings
        self._reaper = reaper
        self._max_records = max_records

    def reconcile(
        self,
        *,
        current_owner: OwnerIdentity,
        is_resumable: Callable[[str], bool] | None = None,
    ) -> HandoffReport:
        retained: list[str] = []
        reaped: list[str] = []
        released: list[str] = []
        resumable_kept: list[str] = []
        conflicts: list[tuple[str, str]] = []
        records = self._bindings.list_all(max_records=self._max_records)
        for binding in records:
            if self._owned_by_current(binding, current_owner):
                retained.append(binding.operation_id)
                continue
            if is_resumable is not None and is_resumable(binding.operation_id):
                resumable_kept.append(binding.operation_id)
                continue
            outcome = self._reaper.reap(binding)
            if outcome.still_alive:
                # Fail closed: the prior generation's worker is still running. Keeping
                # its binding preserves the ownership record, so the new generation
                # cannot claim work that is still producing side effects.
                conflicts.append(
                    (
                        binding.operation_id,
                        f"worker pid={binding.child_pid} survived termination: {outcome.detail}",
                    )
                )
                continue
            if not self._bindings.delete_if_unchanged(binding):
                # The binding was replaced between the scan and the release, so it is
                # no longer the record we evaluated; never delete a newer owner's.
                conflicts.append(
                    (binding.operation_id, "binding was replaced during reconciliation")
                )
                continue
            if outcome.reaped:
                reaped.append(binding.operation_id)
            else:
                released.append(binding.operation_id)
        return HandoffReport(
            scanned=len(records),
            retained=tuple(retained),
            reaped=tuple(reaped),
            released=tuple(released),
            resumable_kept=tuple(resumable_kept),
            conflicts=tuple(conflicts),
        )

    @staticmethod
    def _owned_by_current(binding: OperationWorkerBinding, owner: OwnerIdentity) -> bool:
        # Prefer the explicit v2 generation token when both sides carry one.
        if binding.owner_generation is not None and owner.generation is not None:
            return binding.owner_generation == owner.generation
        # Fall back to process identity: same pid AND same start token. A recycled
        # pid with a different start token is a different process -> not owned.
        if binding.server_pid != owner.server_pid:
            return False
        if binding.server_start_token is None or owner.server_start_token is None:
            # Without a start token on either side we cannot prove same-process
            # identity; treat as not-owned so the binding is reconciled, not adopted.
            return False
        return binding.server_start_token == owner.server_start_token
