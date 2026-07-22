"""Durable operation and receipt coordination for runtime activation."""

from __future__ import annotations

import contextlib
from dataclasses import dataclass

from ...domain.durable_state import StateEnvelope
from ...domain.errors import ErrorCode, RepoForgeError
from ...domain.operation_task import (
    OperationRetryability,
    OperationSnapshotBinding,
    OperationState,
    OperationTask,
    new_operation_task,
    transition_operation,
)
from ...domain.runtime_activation import (
    RuntimeActivationClassification,
    RuntimeActivationIdentity,
    RuntimeActivationReceipt,
    new_runtime_activation_receipt,
    transition_runtime_activation_receipt,
)
from ...ports.clock import Clock
from ...ports.ids import IdGenerator
from ...ports.operation_store import OperationStore
from ...ports.runtime_activation_store import RuntimeActivationStore


@dataclass(frozen=True, slots=True)
class RuntimeActivationAttempt:
    operation: OperationTask
    receipt: StateEnvelope[RuntimeActivationReceipt]


class RuntimeActivationJournal:
    """Create and transition activation operation/receipt records without an app graph."""

    def __init__(
        self,
        *,
        operations: OperationStore,
        receipts: RuntimeActivationStore,
        ids: IdGenerator,
        clock: Clock,
    ) -> None:
        self._operations = operations
        self._receipts = receipts
        self._ids = ids
        self._clock = clock

    def begin(
        self,
        *,
        target: RuntimeActivationIdentity,
        previous: RuntimeActivationIdentity | None,
        continuation_reference: str | None = None,
    ) -> RuntimeActivationAttempt:
        now = self._clock.now_iso()
        operation_id = f"op-{self._ids.new_hex(24)}"
        receipt_id = f"receipt-{self._ids.new_hex(24)}"
        correlation_id = self._ids.new_hex(24)
        operation = self._operations.create(
            new_operation_task(
                operation_id=operation_id,
                kind="runtime_activation",
                phase="accepted",
                now=now,
                cancel_supported=False,
                snapshot_binding=OperationSnapshotBinding(
                    config_generation=target.config_generation
                ),
            )
        )
        receipt = new_runtime_activation_receipt(
            receipt_id=receipt_id,
            operation_id=operation_id,
            accepted_identity=target,
            previous_identity=previous,
            continuation_reference=continuation_reference,
            correlation_id=correlation_id,
            accepted_at=now,
        )
        try:
            persisted = self._receipts.create(receipt)
        except Exception as exc:
            failed = transition_operation(
                operation,
                OperationState.FAILED,
                now=self._clock.now_iso(),
                error_code=ErrorCode.STATE_PERSISTENCE_FAILED.value,
                error_message="Runtime activation receipt persistence failed",
                retryability=OperationRetryability.AUTOMATIC,
            )
            with contextlib.suppress(Exception):
                self._operations.save(failed, expected_updated_at=operation.updated_at)
            raise RepoForgeError(
                "STATE_PERSISTENCE_FAILED: runtime activation receipt was not persisted",
                code=ErrorCode.STATE_PERSISTENCE_FAILED,
                retryable=True,
                unchanged_state=("No runtime candidate was constructed or activated.",),
                details={"operation_id": operation_id},
            ) from exc
        return RuntimeActivationAttempt(operation, persisted)

    def _load(self, receipt_id: str) -> RuntimeActivationAttempt:
        receipt = self._receipts.read(receipt_id)
        if receipt is None:
            raise RepoForgeError(
                f"Runtime activation receipt not found: {receipt_id}",
                code=ErrorCode.STATE_NOT_FOUND,
            )
        operation = self._operations.read(receipt.value.operation_id)
        if operation is None:
            raise RepoForgeError(
                f"Runtime activation operation not found: {receipt.value.operation_id}",
                code=ErrorCode.OPERATION_NOT_FOUND,
            )
        return RuntimeActivationAttempt(operation, receipt)

    def mark_building(self, receipt_id: str) -> RuntimeActivationAttempt:
        current = self._load(receipt_id)
        now = self._clock.now_iso()
        operation = current.operation
        if operation.state is OperationState.PENDING:
            running = transition_operation(operation, OperationState.RUNNING, now=now)
            operation = self._operations.save(
                running, expected_updated_at=current.operation.updated_at
            )
        receipt = self._receipts.save(
            transition_runtime_activation_receipt(
                current.receipt.value,
                RuntimeActivationClassification.BUILDING,
                now=now,
            ),
            expected_revision=current.receipt.revision,
        )
        return RuntimeActivationAttempt(operation, receipt)

    def mark_effect(self, receipt_id: str) -> RuntimeActivationAttempt:
        current = self._load(receipt_id)
        receipt = self._receipts.save(
            transition_runtime_activation_receipt(
                current.receipt.value,
                current.receipt.value.classification,
                now=self._clock.now_iso(),
                effect_boundary_crossed=True,
            ),
            expected_revision=current.receipt.revision,
        )
        return RuntimeActivationAttempt(current.operation, receipt)

    def complete(
        self,
        receipt_id: str,
        *,
        classification: RuntimeActivationClassification,
        active_identity: RuntimeActivationIdentity,
    ) -> RuntimeActivationAttempt:
        if classification not in {
            RuntimeActivationClassification.HOT_RELOAD,
            RuntimeActivationClassification.RESTART_FALLBACK,
            RuntimeActivationClassification.ACTIVE_BUT_CLIENT_STALE,
            RuntimeActivationClassification.ROLLED_BACK,
        }:
            raise ValueError("Runtime activation completion classification is not successful")
        current = self._load(receipt_id)
        now = self._clock.now_iso()
        operation = current.operation
        if operation.state is OperationState.PENDING:
            operation = self._operations.save(
                transition_operation(operation, OperationState.RUNNING, now=now),
                expected_updated_at=operation.updated_at,
            )
        receipt = self._receipts.save(
            transition_runtime_activation_receipt(
                current.receipt.value,
                classification,
                now=now,
                active_identity=active_identity,
                effect_boundary_crossed=True,
            ),
            expected_revision=current.receipt.revision,
        )
        result_reference = f"runtime-activation:{receipt_id}"
        succeeded = transition_operation(
            operation,
            OperationState.SUCCEEDED,
            now=self._clock.now_iso(),
            result_reference=result_reference,
            receipt_id=receipt_id,
        )
        operation = self._operations.save(succeeded, expected_updated_at=operation.updated_at)
        return RuntimeActivationAttempt(operation, receipt)

    def fail(
        self,
        receipt_id: str,
        *,
        error_code: str,
        error_message: str,
        active_identity: RuntimeActivationIdentity | None = None,
        effect_boundary_crossed: bool | None = None,
        retryability: OperationRetryability = OperationRetryability.MANUAL,
    ) -> RuntimeActivationAttempt:
        current = self._load(receipt_id)
        now = self._clock.now_iso()
        operation = current.operation
        if operation.state is OperationState.PENDING:
            operation = self._operations.save(
                transition_operation(operation, OperationState.RUNNING, now=now),
                expected_updated_at=operation.updated_at,
            )
        receipt = self._receipts.save(
            transition_runtime_activation_receipt(
                current.receipt.value,
                RuntimeActivationClassification.RELOAD_FAILED,
                now=now,
                active_identity=active_identity,
                effect_boundary_crossed=effect_boundary_crossed,
                error_code=error_code,
                error_message=error_message,
            ),
            expected_revision=current.receipt.revision,
        )
        failed = transition_operation(
            operation,
            OperationState.FAILED,
            now=self._clock.now_iso(),
            receipt_id=receipt_id,
            error_code=error_code,
            error_message=error_message,
            retryability=retryability,
        )
        operation = self._operations.save(failed, expected_updated_at=operation.updated_at)
        return RuntimeActivationAttempt(operation, receipt)


__all__ = ["RuntimeActivationAttempt", "RuntimeActivationJournal"]
