"""Deterministic startup maintenance for durable operations."""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from datetime import datetime, timedelta

from ...domain.errors import ErrorCode, RepoForgeError
from ...domain.operation_task import TERMINAL_OPERATION_STATES, OperationState
from ...domain.operation_worker import OperationWorkerBinding
from ...ports.process_reaper import ProcessReaper
from ...ports.worker_binding_store import WorkerBindingStore
from .manager import OperationManager


@dataclass(frozen=True, slots=True)
class OperationRecoveryReport:
    scanned: int
    orphaned: int
    expired: int
    deleted: int
    conflicts: int
    reaped: int
    bindings_pruned: int
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
    """Reap any detached child bound to this operation and describe the outcome.

    Returns the human-readable reason and whether a child was actually reaped.
    Never raises: a corrupt binding or a signalling failure must not stop
    startup recovery.
    """
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
    """Drop worker bindings whose operation is missing or already terminal."""
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
    resumable_kinds: frozenset[str] = frozenset(),
    worker_bindings: WorkerBindingStore | None = None,
    reaper: ProcessReaper | None = None,
) -> OperationRecoveryReport:
    """Expire due work, reap+orphan unrecoverable running work, and prune old terminals."""
    if retention_seconds < 0:
        raise ValueError("retention_seconds must be non-negative")
    now_dt = _timestamp(now)
    page = manager.list_records(max_records=2_000)
    orphaned = 0
    expired = 0
    deleted = 0
    conflicts = 0
    reaped = 0
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
        scan_truncated=page.scan_truncated,
    )
