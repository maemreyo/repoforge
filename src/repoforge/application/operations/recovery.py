"""Deterministic startup maintenance for durable operations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from ...domain.errors import ErrorCode, RepoForgeError
from ...domain.operation_task import TERMINAL_OPERATION_STATES, OperationState
from .manager import OperationManager


@dataclass(frozen=True, slots=True)
class OperationRecoveryReport:
    scanned: int
    orphaned: int
    expired: int
    deleted: int
    conflicts: int
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
    resumable_kinds: frozenset[str] = frozenset(),
) -> OperationRecoveryReport:
    """Expire due work, orphan unrecoverable running work, and prune old terminals."""
    if retention_seconds < 0:
        raise ValueError("retention_seconds must be non-negative")
    now_dt = _timestamp(now)
    page = manager.list_records(max_records=2_000)
    orphaned = 0
    expired = 0
    deleted = 0
    conflicts = 0
    cutoff = now_dt - timedelta(seconds=retention_seconds)

    for task in page.records:
        try:
            if task.state in TERMINAL_OPERATION_STATES:
                if _timestamp(task.updated_at) < cutoff:
                    manager.delete(task.operation_id)
                    deleted += 1
                continue
            if task.expires_at is not None and _timestamp(task.expires_at) <= now_dt:
                manager.expire(task.operation_id, now=now)
                expired += 1
                continue
            if task.state is OperationState.RUNNING and task.kind not in resumable_kinds:
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
        scan_truncated=page.scan_truncated,
    )
