"""Shared worker-lifecycle outcome application — the single authority (F-010).

The reviewed split-brain came from two independent writers: the reconciler
terminalized the canonical ProcessLease on one path, normal termination updated
only the ExecutionWorkerBinding projection on another, and a process proven alive
after SIGKILL was recorded as TERMINATED. ``apply_outcome`` is the ONE place a
reaping outcome becomes durable state; the reconciler and normal termination both
call it with the same vocabulary:

    reclaimed         -> RUNNING -> TERMINATING -> TERMINATED, then archived
    already_gone      -> RUNNING -> TERMINATING -> TERMINATED, then archived
    survived_kill     -> RUNNING/TERMINATING -> KILLED        (stays active)
    refused_unproven  -> READY/RUNNING -> UNPROVEN            (stays active)

KILLED and UNPROVEN stay active concerns: they become TERMINATED only on a later
pass that proves the process and its group are gone (a terminal outcome above).
A process the reconciler cannot prove dead is never recorded as TERMINATED.

Persistence failures are never suppressed here: each part (canonical lease,
binding projection, shadow mirror, terminal archive) reports whether it durably
landed, so a caller can never claim a lifecycle completed that the registry did
not record. The canonical ProcessLease is the safety authority (P0-1); the
binding is a derived projection that may legitimately be absent (a binding-less
lease is still a reclaimable concern), so a missing binding never fails the
authoritative transition -- only the lease write can. The shadow is a replica
and the archive is maintenance, both reported but never able to fail the
authoritative transition.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from ...domain.durable_state import Revision
from ...domain.errors import ConfigError
from ...domain.process_lease import (
    ProcessLease,
    ProcessLeaseStatus,
    abort_intent,
    begin_termination,
    confirm_terminated,
    mark_unproven,
    survive_kill,
)
from ...ports.execution_worker_store import ExecutionWorkerBindingStore
from ...ports.process_lease_store import LeaseShadowStore, ProcessLeaseStore
from .execution_worker_concerns import (
    TERMINAL_OUTCOMES,
    WORKER_LIFECYCLE_OUTCOMES,
)

NowIso = Callable[[], str]


@dataclass(frozen=True, slots=True)
class LifecycleApplyResult:
    """What one ``apply_outcome`` durably changed, part by part."""

    worker_id: str
    outcome: str
    binding_updated: bool
    lease_updated: bool
    shadow_updated: bool
    archived: bool
    detail: str

    @property
    def persisted(self) -> bool:
        """Both authoritative parts (binding projection and canonical lease) landed."""
        return self.binding_updated and self.lease_updated


class WorkerLifecycleStore:
    """Apply a reaping outcome to the binding, the lease, and the shadow together."""

    def __init__(
        self,
        *,
        bindings: ExecutionWorkerBindingStore,
        leases: ProcessLeaseStore | None = None,
        shadow: LeaseShadowStore | None = None,
        now_iso: NowIso | None = None,
        binding_only: bool = False,
    ) -> None:
        self._bindings = bindings
        self._leases = leases
        self._shadow = shadow
        self._now_iso = now_iso
        #: ``binding_only`` is the explicit opt-out for embedders with NO canonical
        #: lease authority at all (lease-less test helpers, minimal embedders). It
        #: is never set in production: ``bootstrap`` always wires the lease store,
        #: so a production lifecycle with a missing or unwired canonical store
        #: fails closed (``persisted=False``) instead of reporting a success the
        #: registry never recorded.
        self._binding_only = binding_only

    def apply_outcome(self, worker_id: str, outcome: str) -> LifecycleApplyResult:
        """Persist one reaping outcome across the binding, lease, and shadow.

        Raises for an unknown outcome (a caller bug must never silently no-op a
        lifecycle claim). Per-part persistence failures are returned, not raised:
        the caller decides whether to block on them, and must never report the
        lifecycle as completed when ``persisted`` is False.

        Write ordering is crash-safe (F-010): the CANONICAL lease is persisted
        first, and the binding projection is only touched when the canonical write
        landed. A terminal outcome therefore can never delete/archive the
        projection before the canonical terminal checkpoint exists -- a lease
        write failure leaves the binding intact as the recovery projection the
        next pass re-applies through, instead of destroying it and stranding the
        worker (a binding-only write is never counted as persisted).
        """
        if outcome not in WORKER_LIFECYCLE_OUTCOMES:
            raise ConfigError(f"WORKER_LIFECYCLE_UNKNOWN_OUTCOME: {outcome!r}")
        lease_ok, lease_detail, shadow_ok, archived = self._apply_lease(worker_id, outcome)
        if lease_ok:
            binding_ok, binding_detail = self._apply_binding(worker_id, outcome)
        else:
            binding_ok, binding_detail = (
                False,
                "binding projection left intact: the canonical lease write failed",
            )
        return LifecycleApplyResult(
            worker_id=worker_id,
            outcome=outcome,
            binding_updated=binding_ok,
            lease_updated=lease_ok,
            shadow_updated=shadow_ok,
            archived=archived,
            detail="; ".join(part for part in (binding_detail, lease_detail) if part),
        )

    def _apply_binding(self, worker_id: str, outcome: str) -> tuple[bool, str]:
        if self._bindings is None:
            return True, ""
        try:
            self._bindings.update_state(worker_id, outcome)
            return True, ""
        except Exception as exc:
            return False, f"binding update failed: {type(exc).__name__}: {exc}"

    def _apply_lease(self, worker_id: str, outcome: str) -> tuple[bool, str, bool, bool]:
        leases = self._leases
        now_iso = self._now_iso
        if leases is None or now_iso is None:
            if self._binding_only:
                # An embedder with no canonical lease authority: the projection is
                # the only durable store, so the lease part is vacuously landed.
                return True, "binding-only mode: no canonical lease store wired", True, False
            return (
                False,
                "PROCESS_LEASE_STORE_UNWIRED: no canonical lease store is wired, so "
                "the lifecycle cannot be persisted durably",
                False,
                False,
            )
        envelope = leases.read(worker_id)
        if envelope is None:
            return (
                False,
                "PROCESS_LEASE_MISSING: no canonical lease for "
                f"worker {worker_id!r}; the lifecycle cannot be persisted durably",
                False,
                False,
            )
        lease = envelope.value
        revision = envelope.revision
        now = now_iso()
        try:
            if outcome in TERMINAL_OUTCOMES:
                return self._terminalize(leases, lease, revision, now=now)
            if outcome == "survived_kill":
                return self._mark_survived_kill(leases, lease, revision, now=now)
            return self._mark_refused_unproven(leases, lease, revision, now=now)
        except Exception as exc:
            return (
                False,
                f"lease update failed: {type(exc).__name__}: {exc}",
                False,
                False,
            )

    def _terminalize(
        self,
        leases: ProcessLeaseStore,
        lease: ProcessLease,
        revision: Revision,
        *,
        now: str,
    ) -> tuple[bool, str, bool, bool]:
        """Advance any active status to TERMINATED and archive the record."""
        if lease.status is ProcessLeaseStatus.ARCHIVED:
            return True, "already archived", True, True
        if lease.status is ProcessLeaseStatus.TERMINATED:
            archived = self._archive(lease.lease_id, revision)
            return True, "already terminal", True, archived
        if lease.status is ProcessLeaseStatus.RUNNING:
            # RUNNING -> TERMINATING -> TERMINATED, each step persisted so a crash
            # between the two leaves a durable TERMINATING record a later pass can
            # finish -- the same two-step pattern the registrar uses for READY ->
            # RUNNING (F-001).
            terminating = begin_termination(lease, updated_at=now)
            saved = leases.save(terminating, expected_revision=revision)
            terminal = confirm_terminated(saved.value, updated_at=now)
            final = leases.save(terminal, expected_revision=saved.revision)
            mirrored = self._mirror(final.value, final.revision)
            archived = self._archive(final.value.lease_id, final.revision)
            return True, "terminalized -> terminated", mirrored, archived
        terminal = self._terminal_lease(lease, now=now)
        saved = leases.save(terminal, expected_revision=revision)
        mirrored = self._mirror(saved.value, saved.revision)
        archived = self._archive(saved.value.lease_id, saved.revision)
        return True, f"terminalized -> {terminal.status.value}", mirrored, archived

    def _mark_survived_kill(
        self,
        leases: ProcessLeaseStore,
        lease: ProcessLease,
        revision: Revision,
        *,
        now: str,
    ) -> tuple[bool, str, bool, bool]:
        """RUNNING/TERMINATING -> KILLED; the process survived SIGKILL."""
        if lease.status not in {
            ProcessLeaseStatus.RUNNING,
            ProcessLeaseStatus.TERMINATING,
        }:
            return True, "already a live concern", True, False
        killed = survive_kill(lease, updated_at=now)
        saved = leases.save(killed, expected_revision=revision)
        return True, "killed", self._mirror(saved.value, saved.revision), False

    def _mark_refused_unproven(
        self,
        leases: ProcessLeaseStore,
        lease: ProcessLease,
        revision: Revision,
        *,
        now: str,
    ) -> tuple[bool, str, bool, bool]:
        """READY/RUNNING -> UNPROVEN; identity could not be proven."""
        if lease.status not in {
            ProcessLeaseStatus.READY,
            ProcessLeaseStatus.RUNNING,
        }:
            return True, "already a live concern", True, False
        unproven = mark_unproven(
            lease,
            updated_at=now,
            error_code="EXECUTION_WORKER_REFUSED_UNPROVEN",
            error_message="the process identity could not be proven; the worker may "
            "still be running and holding locks",
        )
        saved = leases.save(unproven, expected_revision=revision)
        return True, "unproven", self._mirror(saved.value, saved.revision), False

    def _terminal_lease(self, lease: ProcessLease, *, now: str) -> ProcessLease:
        """The exhaustive status -> TERMINATED path; an unknown status raises."""
        status = lease.status
        if status is ProcessLeaseStatus.REGISTERED:
            return abort_intent(
                lease,
                updated_at=now,
                error_code="EXECUTION_WORKER_RECLAIMED",
                error_message="pre-spawn intent reclaimed",
            )
        if status is ProcessLeaseStatus.READY:
            unproven = mark_unproven(
                lease,
                updated_at=now,
                error_code="EXECUTION_WORKER_RECLAIMED",
                error_message="unproven claim reclaimed",
            )
            return confirm_terminated(unproven, updated_at=now)
        if status is ProcessLeaseStatus.UNPROVEN:
            return confirm_terminated(lease, updated_at=now)
        if status is ProcessLeaseStatus.RUNNING:
            terminating = begin_termination(lease, updated_at=now)
            return confirm_terminated(terminating, updated_at=now)
        if status is ProcessLeaseStatus.TERMINATING:
            return confirm_terminated(lease, updated_at=now)
        if status is ProcessLeaseStatus.KILLED:
            return confirm_terminated(lease, updated_at=now)
        if status is ProcessLeaseStatus.QUARANTINED:
            return confirm_terminated(lease, updated_at=now)
        raise ConfigError(
            f"WORKER_LIFECYCLE_ILLEGAL_TERMINALIZE: no terminal path from {status.value}"
        )

    def _mirror(self, lease: ProcessLease, revision: Revision) -> bool:
        """Mirror one authoritative lease write into the shadow, never failing it."""
        if self._shadow is None:
            return True
        try:
            self._shadow.write_shadow(lease, revision)
            return True
        except Exception:
            return False

    def _archive(self, lease_id: str, revision: Revision) -> bool:
        """Archive+delete one terminal active record; best-effort maintenance.

        After the canonical archive lands, the shadow row is deleted too, so a
        normal termination cannot leave a stale TERMINATED shadow row that makes
        parity report ``only_in_shadow`` forever. Both steps are best-effort: a
        failure leaves the record/row for the next ``collect_terminal`` pass.
        """
        if self._leases is None:
            return False
        archive = getattr(self._leases, "archive_terminal", None)
        if archive is None:
            return False
        try:
            archived = bool(archive(lease_id, expected_revision=revision))
        except Exception:
            return False
        if archived:
            self._drop_shadow(lease_id)
        return archived

    def _drop_shadow(self, lease_id: str) -> None:
        """Remove one mirror row after the canonical terminal archive landed."""
        if self._shadow is None:
            return
        delete = getattr(self._shadow, "delete_shadow", None)
        if delete is None:
            return
        try:
            delete(lease_id)
        except Exception:
            return

    def collect_shadow_terminal(self, *, max_records: int = 5_000) -> int:
        """Bounded sweep removing terminal shadow rows so parity converges.

        Called by the mutating reconciliation pass as maintenance, never by a
        read-only preflight. A failure propagates so the caller can report it --
        shadow drift is evidence, never a silent no-op.
        """
        if self._shadow is None:
            return 0
        collect = getattr(self._shadow, "collect_terminal", None)
        if collect is None:
            return 0
        return int(collect(max_records=max_records))


__all__ = [
    "LifecycleApplyResult",
    "WorkerLifecycleStore",
]
