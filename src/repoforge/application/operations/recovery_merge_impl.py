"""Deterministic startup maintenance for durable operations."""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from ...domain.errors import ErrorCode, RepoForgeError
from ...domain.operation_task import TERMINAL_OPERATION_STATES, OperationState, OperationTask
from ...domain.operation_worker import OperationWorkerBinding
from ...ports.process_reaper import ProcessReaper
from ...ports.worker_binding_store import WorkerBindingStore
from .manager import OperationManager

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


def _timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("operation timestamp must include a timezone")
    return parsed


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
        bindings = worker_bindings.list_all()
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
