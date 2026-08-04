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

#275 also drives this reconciler on the normal execution-worker admit path (not only the
tunnel-seam swap): a generation-bound worker must reconcile before claiming durable work
so two generations never both admit the same operation.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from ...domain.operation_worker import OperationWorkerBinding
from ...ports.process_reaper import ProcessReaper
from ...ports.worker_binding_store import WorkerBindingStore

#: Exit code when admit-time handoff cannot transfer ownership (prior worker still live,
#: scan incomplete, or unreadable binding). The worker must not start admitting work.
EXIT_HANDOFF_CONFLICT = 7

#: Audit action for generation handoff reconciliation (activation and admit paths).
HANDOFF_AUDIT_ACTION = "generation_handoff_reconcile"


class _AuditRecorder(Protocol):
    def record(self, action: str, *, success: bool, details: dict[str, Any]) -> None: ...


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
    transferred. ``scan_complete`` reports whether every binding was seen; a
    truncated scan or unreadable records means unseen prior-generation workers may
    still run, which must also fail the handoff. ``ok`` is False whenever any
    conflict was recorded, the scan was incomplete, or a record was unreadable,
    and the caller must abort the handoff rather than let a second generation
    claim that work.
    """

    scanned: int
    retained: tuple[str, ...] = ()
    reaped: tuple[str, ...] = ()
    released: tuple[str, ...] = ()
    resumable_kept: tuple[str, ...] = ()
    conflicts: tuple[tuple[str, str], ...] = ()
    scan_complete: bool = True
    unreadable: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.conflicts and self.scan_complete and not self.unreadable

    def as_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "scanned": self.scanned,
            "scan_complete": self.scan_complete,
            "unreadable": list(self.unreadable),
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
        page = self._bindings.list_all(max_records=self._max_records)
        if not page.scan_complete:
            conflicts.append(
                (
                    "<store>",
                    "worker-binding scan exceeded the record budget; unseen "
                    "bindings may still own running prior-generation workers",
                )
            )
        for unreadable_id in page.unreadable_ids:
            conflicts.append((unreadable_id, "worker binding record could not be decoded"))
        for binding in page.records:
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
            scanned=len(page.records),
            retained=tuple(retained),
            reaped=tuple(reaped),
            released=tuple(released),
            resumable_kept=tuple(resumable_kept),
            conflicts=tuple(conflicts),
            scan_complete=page.scan_complete,
            unreadable=page.unreadable_ids,
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


@dataclass(frozen=True, slots=True)
class AdmitHandoffResult:
    """Outcome of admit-time handoff: whether the worker may claim durable work."""

    report: HandoffReport
    exit_code: int | None


def reconcile_before_admit(
    *,
    reconciler: GenerationHandoffReconciler,
    current_owner: OwnerIdentity,
    audit: _AuditRecorder | None = None,
    is_resumable: Callable[[str], bool] | None = None,
) -> AdmitHandoffResult:
    """Reconcile prior-generation bindings before this generation admits work (#275).

    Emits a handoff audit event (success or fail-closed). When ``report.ok`` is False the
    worker must exit with ``EXIT_HANDOFF_CONFLICT`` rather than claim queue items that a
    surviving prior-generation worker may still be executing.
    """
    report = reconciler.reconcile(current_owner=current_owner, is_resumable=is_resumable)
    if audit is not None:
        details: dict[str, Any] = {
            **report.as_dict(),
            "generation": current_owner.generation,
            "server_pid": current_owner.server_pid,
            "path": "admit",
        }
        with contextlib.suppress(Exception):
            audit.record(HANDOFF_AUDIT_ACTION, success=report.ok, details=details)
    if not report.ok:
        return AdmitHandoffResult(report=report, exit_code=EXIT_HANDOFF_CONFLICT)
    return AdmitHandoffResult(report=report, exit_code=None)
