"""Compact public views for durable operations."""

from __future__ import annotations

from dataclasses import dataclass

from ...domain.operation_task import OperationTask


@dataclass(frozen=True, slots=True)
class OperationProgressView:
    current: int
    total: int | None
    unit: str | None
    message: str | None


@dataclass(frozen=True, slots=True)
class OperationSnapshotView:
    head_sha: str | None
    workspace_fingerprint: str | None
    config_generation: int | None
    evidence_snapshot_id: str | None


@dataclass(frozen=True, slots=True)
class OperationSummary:
    operation_id: str
    kind: str
    state: str
    phase: str
    progress: OperationProgressView
    task_id: str | None
    workspace_id: str | None
    snapshot_binding: OperationSnapshotView | None
    result_reference: str | None
    error_code: str | None
    error_message: str | None
    retryability: str
    cancel_supported: bool
    cancellation_requested_at: str | None
    created_at: str
    updated_at: str
    expires_at: str | None


def operation_summary(task: OperationTask) -> OperationSummary:
    return OperationSummary(
        operation_id=task.operation_id,
        kind=task.kind,
        state=task.state.value,
        phase=task.phase,
        progress=OperationProgressView(
            task.progress_current,
            task.progress_total,
            task.progress_unit,
            task.progress_message,
        ),
        task_id=task.task_id,
        workspace_id=task.workspace_id,
        snapshot_binding=(
            OperationSnapshotView(
                task.snapshot_binding.head_sha,
                task.snapshot_binding.workspace_fingerprint,
                task.snapshot_binding.config_generation,
                task.snapshot_binding.evidence_snapshot_id,
            )
            if task.snapshot_binding is not None
            else None
        ),
        result_reference=task.result_reference,
        error_code=task.error_code,
        error_message=task.error_message,
        retryability=task.retryability.value,
        cancel_supported=task.cancel_supported,
        cancellation_requested_at=task.cancellation_requested_at,
        created_at=task.created_at,
        updated_at=task.updated_at,
        expires_at=task.expires_at,
    )
