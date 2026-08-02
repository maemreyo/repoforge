"""Deterministic startup maintenance for durable operations."""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from ...domain.errors import ErrorCode, RepoForgeError
from ...domain.operation_task import (
    TERMINAL_OPERATION_STATES,
    OperationRetryability,
    OperationState,
    OperationTask,
)
from ...domain.operation_work import OperationWorkItem, OperationWorkState, requeue_unstarted_work
from ...domain.operation_worker import OperationWorkerBinding
from ...ports.operation_work_queue import OperationWorkQueue
from ...ports.process_reaper import ProcessReaper
from ...ports.worker_binding_store import WorkerBindingStore
from .manager import OperationManager

# How long "operation record present, work item absent" is read as an admission still in
# progress rather than as a crashed one. Admission performs two durable writes in a fixed
# order, and this bounds the window between them; it is generously wider than that window
# because the cost of waiting one recovery pass is nothing, while failing a live admission
# loses real work.
_ADMISSION_GRACE_SECONDS = 60

RunningLivenessProbe = Callable[[OperationTask], bool | None]


@dataclass(frozen=True, slots=True)
class OperationRecoveryReport:
    scanned: int
    orphaned: int
    expired: int
    deleted: int
    conflicts: int
    reaped: int
    bindings_pruned: int
    missing_result_references: int
    missing_receipt_references: int
    retained_for_receipt: int
    operation_record_inconsistencies: int
    legacy_operation_records: int
    scan_truncated: bool


@dataclass(frozen=True, slots=True)
class OperationWorkRecoveryReport:
    scanned: int
    requeued: int
    orphaned: int
    missing_work: int
    orphan_work: int
    stale_generation: int
    cancelled: int
    conflicts: int
    scan_truncated: bool


def _timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("operation timestamp must include a timezone")
    return parsed


def _admission_window_elapsed(created_at: str, now: datetime) -> bool:
    """Has an operation existed long enough that a missing work item means it crashed?

    Fails closed on an unreadable timestamp: a record whose age cannot be established is
    not treated as an in-flight admission, because that would make it un-recoverable
    forever.
    """
    try:
        created = _timestamp(created_at)
    except ValueError:
        return True
    return now - created >= timedelta(seconds=_ADMISSION_GRACE_SECONDS)


def _reap_and_describe(
    operation_id: str,
    *,
    worker_bindings: WorkerBindingStore | None,
    reaper: ProcessReaper | None,
) -> tuple[str, bool]:
    """Reap a detached child bound to an operation without blocking recovery."""
    base = "the runtime process that owned it is gone"
    if worker_bindings is None:
        return base, False
    binding = None
    with contextlib.suppress(RepoForgeError):
        binding = worker_bindings.get(operation_id)
    if binding is None:
        return f"{base}; no live worker binding was recorded", False
    if reaper is None:
        detail = f"{base}; child worker pgid={binding.child_pgid} not reaped (no reaper configured)"
        return detail, False
    reaped = False
    outcome_detail = "reap skipped"
    with contextlib.suppress(Exception):
        outcome = reaper.reap(binding)
        reaped = outcome.reaped and outcome.attempted
        outcome_detail = outcome.detail
    with contextlib.suppress(RepoForgeError):
        worker_bindings.delete(operation_id)
    return f"{base}; child worker pgid={binding.child_pgid} reap: {outcome_detail}", reaped


def _prune_bindings(
    manager: OperationManager,
    worker_bindings: WorkerBindingStore | None,
) -> int:
    """Drop bindings whose operation is missing or already terminal."""
    if worker_bindings is None:
        return 0
    pruned = 0
    bindings: tuple[OperationWorkerBinding, ...] = ()
    with contextlib.suppress(RepoForgeError):
        bindings = worker_bindings.list_all().records
    for binding in bindings:
        stale = True
        with contextlib.suppress(RepoForgeError):
            record = manager.status(binding.operation_id)
            stale = record.state in TERMINAL_OPERATION_STATES
        if stale:
            with contextlib.suppress(RepoForgeError):
                worker_bindings.delete(binding.operation_id)
                pruned += 1
    return pruned


def reap_running_background(
    manager: OperationManager,
    *,
    now: str,
    reason: str,
    resumable_kinds: frozenset[str] = frozenset(),
    worker_bindings: WorkerBindingStore | None = None,
    reaper: ProcessReaper | None = None,
    work_queue: OperationWorkQueue | None = None,
) -> int:
    """Reap + orphan every RUNNING background op this process still owns.

    Used at graceful runtime shutdown so a detached child cannot outlive the
    process. Work with a durable sidecar is skipped: a separate execution worker
    owns that child and outlives this process, so reaping it here would kill a
    live run and orphan a perfectly recoverable operation. Everything else --
    including an in-process background run whose child dies with this process --
    is still reaped here. Best-effort and idempotent: already-terminal ops are
    skipped and a stale conflict on one record never stops the sweep. Returns how
    many ops were transitioned to orphaned.
    """
    transitioned = 0
    page = manager.list_records(max_records=2_000)
    for task in page.records:
        if task.state is not OperationState.RUNNING or task.kind in resumable_kinds:
            continue
        if work_queue is not None:
            owned_elsewhere = False
            with contextlib.suppress(RepoForgeError):
                owned_elsewhere = work_queue.read(task.operation_id) is not None
            if owned_elsewhere:
                continue
        detail, _ = _reap_and_describe(
            task.operation_id,
            worker_bindings=worker_bindings,
            reaper=reaper,
        )
        try:
            manager.orphan(
                task.operation_id,
                error_message=f"{reason}: {detail}.",
                now=now,
            )
            transitioned += 1
        except RepoForgeError as exc:
            if exc.code is ErrorCode.OPERATION_STALE:
                continue
            raise
    return transitioned


def recover_operations(
    manager: OperationManager,
    *,
    now: str,
    retention_seconds: int = 7 * 24 * 60 * 60,
    running_stale_seconds: int = 0,
    resumable_kinds: frozenset[str] = frozenset(),
    running_liveness: RunningLivenessProbe | None = None,
    worker_bindings: WorkerBindingStore | None = None,
    reaper: ProcessReaper | None = None,
) -> OperationRecoveryReport:
    """Expire due work, orphan unrecoverable running work, and prune old terminals."""
    if retention_seconds < 0:
        raise ValueError("retention_seconds must be non-negative")
    if running_stale_seconds < 0:
        raise ValueError("running_stale_seconds must be non-negative")
    now_dt = _timestamp(now)
    page = manager.list_records(max_records=2_000)
    orphaned = 0
    expired = 0
    deleted = 0
    conflicts = 0
    reaped = 0
    missing_result_references = 0
    missing_receipt_references = 0
    retained_for_receipt = 0
    operation_record_inconsistencies = 0
    legacy_operation_records = 0
    cutoff = now_dt - timedelta(seconds=retention_seconds)
    running_cutoff = now_dt - timedelta(seconds=running_stale_seconds)

    for task in page.records:
        if task.record_consistency == "record_inconsistent":
            operation_record_inconsistencies += 1
        if task.record_provenance == "legacy_migrated":
            legacy_operation_records += 1
        try:
            if task.state in TERMINAL_OPERATION_STATES:
                if (
                    task.result_reference is not None
                    and manager.ctx.operation_result_store is not None
                    and manager.ctx.operation_result_store.read(task.operation_id) is None
                ):
                    missing_result_references += 1
                if (
                    task.receipt_id is not None
                    and manager.ctx.effect_receipts is not None
                    and manager.ctx.effect_receipts.read(task.receipt_id) is None
                ):
                    missing_receipt_references += 1
                if _timestamp(task.updated_at) < cutoff:
                    receipt_exists = (
                        task.receipt_id is not None
                        and manager.ctx.effect_receipts is not None
                        and manager.ctx.effect_receipts.read(task.receipt_id) is not None
                    )
                    if receipt_exists:
                        retained_for_receipt += 1
                        continue
                    if manager.ctx.operation_result_store is not None:
                        manager.ctx.operation_result_store.delete(task.operation_id)
                    manager.delete(task.operation_id)
                    deleted += 1
                continue
            if task.expires_at is not None and _timestamp(task.expires_at) <= now_dt:
                manager.expire(task.operation_id, now=now)
                expired += 1
                continue
            if (
                task.state is OperationState.RUNNING
                and task.kind not in resumable_kinds
                and task.lease_expires_at is not None
                and _timestamp(task.lease_expires_at) <= now_dt
            ):
                reason, did_reap = _reap_and_describe(
                    task.operation_id,
                    worker_bindings=worker_bindings,
                    reaper=reaper,
                )
                if did_reap:
                    reaped += 1
                manager.orphan(
                    task.operation_id,
                    error_code="OPERATION_OWNERSHIP_EXPIRED",
                    error_message=f"OPERATION_OWNERSHIP_EXPIRED: {reason}.",
                    now=now,
                )
                orphaned += 1
                continue
            if task.state is OperationState.RUNNING and task.kind not in resumable_kinds:
                liveness = running_liveness(task) if running_liveness is not None else None
                if liveness is True:
                    continue
                if (
                    liveness is False
                    or running_stale_seconds == 0
                    or _timestamp(task.updated_at) <= running_cutoff
                ):
                    reason, did_reap = _reap_and_describe(
                        task.operation_id,
                        worker_bindings=worker_bindings,
                        reaper=reaper,
                    )
                    if did_reap:
                        reaped += 1
                    manager.orphan(
                        task.operation_id,
                        error_message=f"OPERATION_WORKER_LOST: {reason}.",
                        now=now,
                    )
                    orphaned += 1
        except RepoForgeError as exc:
            if exc.code is ErrorCode.OPERATION_STALE:
                conflicts += 1
                continue
            raise

    bindings_pruned = _prune_bindings(manager, worker_bindings)

    return OperationRecoveryReport(
        scanned=len(page.records),
        orphaned=orphaned,
        expired=expired,
        deleted=deleted,
        conflicts=conflicts,
        reaped=reaped,
        bindings_pruned=bindings_pruned,
        missing_result_references=missing_result_references,
        missing_receipt_references=missing_receipt_references,
        retained_for_receipt=retained_for_receipt,
        operation_record_inconsistencies=operation_record_inconsistencies,
        legacy_operation_records=legacy_operation_records,
        scan_truncated=page.scan_truncated,
    )


def _contain_work_child(
    item: OperationWorkItem,
    *,
    worker_bindings: WorkerBindingStore | None,
    reaper: ProcessReaper | None,
) -> tuple[OperationWorkerBinding | None, str]:
    if worker_bindings is None or reaper is None:
        return None, "durable child containment is not configured"
    binding = worker_bindings.get(item.operation_id)
    if binding is None:
        return None, "no durable child identity is available"
    if binding.owner_id is not None and binding.owner_id != item.owner_id:
        return None, "child binding owner does not match the claimed work owner"
    if binding.attempt is not None and binding.attempt != item.attempt:
        return None, "child binding attempt does not match the claimed work attempt"
    outcome = reaper.reap(binding)
    if not outcome.reaped or outcome.still_alive:
        return None, outcome.detail
    return binding, outcome.detail


def recover_operation_work(
    manager: OperationManager,
    queue: OperationWorkQueue,
    *,
    now: str,
    expected_config_generation: int | None = None,
    worker_bindings: WorkerBindingStore | None = None,
    reaper: ProcessReaper | None = None,
) -> OperationWorkRecoveryReport:
    """Reconcile durable operation records with their private work sidecars."""
    now_dt = _timestamp(now)
    work_page = queue.list_records(max_records=2_000)
    operation_page = manager.list_records(max_records=2_000)
    operations = {task.operation_id: task for task in operation_page.records}
    work_ids = {item.operation_id for item in work_page.records}
    requeued = 0
    orphaned = 0
    missing_work = 0
    orphan_work = 0
    stale_generation = 0
    cancelled = 0
    conflicts = 0

    for item in work_page.records:
        operation = operations.get(item.operation_id)
        if operation is None or operation.state in TERMINAL_OPERATION_STATES:
            queue.delete(item.operation_id)
            orphan_work += 1
            continue
        if operation.cancellation_requested_at is not None:
            contained_binding = None
            if item.child_started:
                contained_binding, _detail = _contain_work_child(
                    item,
                    worker_bindings=worker_bindings,
                    reaper=reaper,
                )
                if contained_binding is None:
                    conflicts += 1
                    continue
            try:
                manager.cancelled(
                    item.operation_id,
                    owner_id=operation.owner_id,
                    now=now,
                )
                queue.delete(item.operation_id)
                if contained_binding is not None and worker_bindings is not None:
                    worker_bindings.delete_if_unchanged(contained_binding)
                cancelled += 1
            except RepoForgeError as exc:
                if exc.code in {ErrorCode.OPERATION_STALE, ErrorCode.STATE_STALE}:
                    conflicts += 1
                    continue
                raise
            continue
        # Only work from an OLDER generation is unrunnable. A `!=` test also failed work
        # from a NEWER one, which is the normal state for a few seconds during a hot reload:
        # the request side swaps to the new generation and admits against it before the
        # supervisor has replaced this worker. That work is perfectly good -- it is simply
        # not this worker's to claim, and the replacement worker will take it -- so failing
        # it destroyed valid work and reported the operator's own config change as an error.
        if (
            expected_config_generation is not None
            and item.request.config_generation < expected_config_generation
        ):
            try:
                detail = (
                    "Durable work was admitted for config generation "
                    f"{item.request.config_generation}, but the active worker is generation "
                    f"{expected_config_generation}."
                )
                contained_binding = None
                if operation.state is OperationState.RUNNING:
                    if item.child_started:
                        contained_binding, _containment_detail = _contain_work_child(
                            item,
                            worker_bindings=worker_bindings,
                            reaper=reaper,
                        )
                        if contained_binding is None:
                            conflicts += 1
                            continue
                    manager.orphan(
                        item.operation_id,
                        error_code="OPERATION_GENERATION_STALE",
                        error_message=detail,
                        now=now,
                    )
                else:
                    manager.fail(
                        item.operation_id,
                        error_code="OPERATION_GENERATION_STALE",
                        error_message=detail,
                        retryability=OperationRetryability.MANUAL,
                        now=now,
                    )
                queue.delete(item.operation_id)
                if contained_binding is not None and worker_bindings is not None:
                    worker_bindings.delete_if_unchanged(contained_binding)
                stale_generation += 1
            except RepoForgeError as exc:
                if exc.code in {ErrorCode.OPERATION_STALE, ErrorCode.STATE_STALE}:
                    conflicts += 1
                    continue
                raise
            continue
        if (
            item.state is OperationWorkState.CLAIMED
            and item.lease_expires_at is not None
            and _timestamp(item.lease_expires_at) <= now_dt
        ):
            try:
                if item.child_started:
                    contained_binding, _containment_detail = _contain_work_child(
                        item,
                        worker_bindings=worker_bindings,
                        reaper=reaper,
                    )
                    if contained_binding is None:
                        conflicts += 1
                        continue
                    manager.orphan(
                        item.operation_id,
                        error_code="OPERATION_WORKER_LOST",
                        error_message="Durable worker lease expired after the child process started.",
                        now=now,
                    )
                    queue.delete(item.operation_id)
                    if worker_bindings is not None:
                        worker_bindings.delete_if_unchanged(contained_binding)
                    orphaned += 1
                else:
                    queued = requeue_unstarted_work(item, now=now)
                    queue.save(queued, expected_updated_at=item.updated_at)
                    if operation.state is OperationState.RUNNING:
                        manager.requeue(item.operation_id, now=now)
                    requeued += 1
            except RepoForgeError as exc:
                if exc.code in {ErrorCode.OPERATION_STALE, ErrorCode.STATE_STALE}:
                    conflicts += 1
                    continue
                raise

    durable_kinds = frozenset(
        {"workspace_run_profile", "workspace_run_adhoc", "workspace_run_diagnostic"}
    )
    # Only an operation still waiting to be claimed proves its sidecar is required: a
    # queued operation without one can never be claimed. A RUNNING record is deliberately
    # excluded -- it is either owned by a live worker that will terminalize it, or already
    # covered by lease-expiry and worker-loss recovery, which carry the exact evidence for
    # that failure.
    #
    # The grace window is required, not defensive padding. Admission writes the operation
    # record before the work item (so a claimable item always has a readable record), which
    # means "record present, item absent" is the NORMAL state for the microseconds between
    # those two writes. Without the window a recovery pass landing inside it would fail a
    # perfectly good admission. Bounded by `created_at`, so it is decided by durable state
    # and an injected `now` rather than by wall-clock luck.
    for operation in operation_page.records:
        if (
            operation.kind in durable_kinds
            and operation.state is OperationState.PENDING
            and operation.phase == "queued"
            and operation.operation_id not in work_ids
            and _admission_window_elapsed(operation.created_at, now_dt)
        ):
            manager.fail(
                operation.operation_id,
                error_code="OPERATION_WORK_MISSING",
                error_message="Durable operation has no recoverable work sidecar.",
                retryability=OperationRetryability.MANUAL,
                now=now,
            )
            missing_work += 1

    return OperationWorkRecoveryReport(
        scanned=len(work_page.records),
        requeued=requeued,
        orphaned=orphaned,
        missing_work=missing_work,
        orphan_work=orphan_work,
        stale_generation=stale_generation,
        cancelled=cancelled,
        conflicts=conflicts,
        scan_truncated=work_page.scan_truncated or operation_page.scan_truncated,
    )
