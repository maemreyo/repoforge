"""Compact public views for durable operations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

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
    result_reference_status: str
    receipt_id: str | None
    receipt_status: str
    error_code: str | None
    error_message: str | None
    retryability: str
    cancel_supported: bool
    cancellation_requested_at: str | None
    created_at: str
    updated_at: str
    expires_at: str | None
    owner_id: str | None
    lease_expires_at: str | None
    schema_version: int
    record_provenance: str
    record_consistency: str
    record_diagnostics: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class OperationStatusView:
    operation_id: str
    kind: str
    state: str
    phase: str
    progress: OperationProgressView
    task_id: str | None
    workspace_id: str | None
    snapshot_binding: OperationSnapshotView | None
    result_reference: str | None
    result_reference_status: str
    receipt_id: str | None
    receipt_status: str
    result: dict[str, Any] | None
    error_code: str | None
    error_message: str | None
    retryability: str
    cancel_supported: bool
    cancellation_requested_at: str | None
    created_at: str
    updated_at: str
    expires_at: str | None
    owner_id: str | None
    lease_expires_at: str | None
    schema_version: int
    record_provenance: str
    record_consistency: str
    record_diagnostics: tuple[str, ...]


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
        result_reference_status=(
            "not_applicable" if task.result_reference is None else "not_checked"
        ),
        receipt_id=task.receipt_id,
        receipt_status="not_applicable" if task.receipt_id is None else "not_checked",
        error_code=task.error_code,
        error_message=task.error_message,
        retryability=task.retryability.value,
        cancel_supported=task.cancel_supported,
        cancellation_requested_at=task.cancellation_requested_at,
        created_at=task.created_at,
        updated_at=task.updated_at,
        expires_at=task.expires_at,
        owner_id=task.owner_id,
        lease_expires_at=task.lease_expires_at,
        schema_version=task.schema_version,
        record_provenance=task.record_provenance,
        record_consistency=task.record_consistency,
        record_diagnostics=task.record_diagnostics,
    )


def operation_status_view(
    task: OperationTask,
    result: dict[str, Any] | None,
    *,
    result_checked: bool = True,
    receipt_checked: bool = True,
    receipt_available: bool = False,
) -> OperationStatusView:
    summary = operation_summary(task)
    result_reference_status = (
        "not_applicable"
        if summary.result_reference is None
        else "not_checked"
        if not result_checked
        else "available"
        if result is not None
        else "missing"
    )
    receipt_status = (
        "not_applicable"
        if summary.receipt_id is None
        else "not_checked"
        if not receipt_checked
        else "available"
        if receipt_available
        else "missing"
    )
    diagnostics = summary.record_diagnostics
    consistency = summary.record_consistency
    if result_reference_status == "missing":
        diagnostics = tuple(sorted(set(diagnostics) | {"missing_result_reference_payload"}))
        consistency = "record_inconsistent"
    if receipt_status == "missing":
        diagnostics = tuple(sorted(set(diagnostics) | {"missing_receipt_reference"}))
        consistency = "record_inconsistent"
    return OperationStatusView(
        operation_id=summary.operation_id,
        kind=summary.kind,
        state=summary.state,
        phase=summary.phase,
        progress=summary.progress,
        task_id=summary.task_id,
        workspace_id=summary.workspace_id,
        snapshot_binding=summary.snapshot_binding,
        result_reference=summary.result_reference,
        result_reference_status=result_reference_status,
        receipt_id=summary.receipt_id,
        receipt_status=receipt_status,
        result=result,
        error_code=summary.error_code,
        error_message=summary.error_message,
        retryability=summary.retryability,
        cancel_supported=summary.cancel_supported,
        cancellation_requested_at=summary.cancellation_requested_at,
        created_at=summary.created_at,
        updated_at=summary.updated_at,
        expires_at=summary.expires_at,
        owner_id=summary.owner_id,
        lease_expires_at=summary.lease_expires_at,
        schema_version=summary.schema_version,
        record_provenance=summary.record_provenance,
        record_consistency=consistency,
        record_diagnostics=diagnostics,
    )
