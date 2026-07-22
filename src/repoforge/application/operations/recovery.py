"""Deterministic startup maintenance for durable operations."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from ...domain.errors import ErrorCode, RepoForgeError
from ...domain.operation_task import TERMINAL_OPERATION_STATES, OperationState, OperationTask
from .manager import OperationManager

RunningLivenessProbe = Callable[[OperationTask], bool | None]


@dataclass(frozen=True, slots=True)
class OperationRecoveryReport:
    scanned: int
    orphaned: int
    expired: int
    deleted: int
    conflicts: int
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


def recover_operations(
    manager: OperationManager,
    *,
    now: str,
    retention_seconds: int = 7 * 24 * 60 * 60,
    running_stale_seconds: int = 0,
    resumable_kinds: frozenset[str] = frozenset(),
    running_liveness: RunningLivenessProbe | None = None,
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
                manager.orphan(
                    task.operation_id,
                    error_code="OPERATION_OWNERSHIP_EXPIRED",
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
                    manager.orphan(task.operation_id, now=now)
                    orphaned += 1
        except RepoForgeError as exc:
            if exc.code is ErrorCode.OPERATION_STALE:
                conflicts += 1
                continue
            raise

    return OperationRecoveryReport(
        scanned=len(page.records),
        orphaned=orphaned,
        expired=expired,
        deleted=deleted,
        conflicts=conflicts,
        missing_result_references=missing_result_references,
        missing_receipt_references=missing_receipt_references,
        retained_for_receipt=retained_for_receipt,
        operation_record_inconsistencies=operation_record_inconsistencies,
        legacy_operation_records=legacy_operation_records,
        scan_truncated=page.scan_truncated,
    )
