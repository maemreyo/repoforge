"""Deterministic startup reconciliation for non-terminal outcome receipts."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from ..domain.errors import ErrorCode, RepoForgeError
from ..domain.execution_receipt import EffectReceiptState, transition_effect_receipt
from ..domain.operation_task import OperationRetryability, OperationState
from ..domain.runtime import RuntimePhase, RuntimeRecord
from ..domain.runtime_activation import (
    RuntimeActivationClassification,
    RuntimeActivationIdentity,
    RuntimeActivationReceipt,
)
from ..ports.operation_store import OperationStore
from ..ports.runtime_activation_store import RuntimeActivationStore
from .context import ApplicationContext
from .operations.manager import OperationManager
from .runtime.activation_journal import RuntimeActivationJournal


def _is_durable_operation_reference(value: str) -> bool:
    suffix = value.removeprefix("op-")
    return (
        len(value) == 27
        and len(suffix) == 24
        and all(char in "0123456789abcdef" for char in suffix)
    )


@dataclass(frozen=True, slots=True)
class RuntimeActivationReconciliationReport:
    scanned: int
    activated: int
    failed_before_effect: int
    unknown: int
    continuation_resumable: int
    unchanged_terminal: int
    scan_truncated: bool


class RuntimeActivationReconciler:
    """Terminalize interrupted activation attempts from durable runtime evidence."""

    def __init__(
        self,
        *,
        journal: RuntimeActivationJournal,
        receipts: RuntimeActivationStore,
        operations: OperationStore,
    ) -> None:
        self._journal = journal
        self._receipts = receipts
        self._operations = operations

    @staticmethod
    def _target_active_identity(
        accepted: RuntimeActivationIdentity,
        runtime: RuntimeRecord,
    ) -> RuntimeActivationIdentity:
        return replace(
            accepted,
            runtime_active_generation=runtime.active_generation,
            process_identity=runtime.process_identity,
            tool_surface_hash=runtime.tool_surface_hash,
            runtime_phase=runtime.phase.value,
        )

    @staticmethod
    def _target_is_active(receipt_generation: int, runtime: RuntimeRecord | None) -> bool:
        return bool(
            runtime is not None
            and runtime.active_generation == receipt_generation
            and runtime.phase in {RuntimePhase.HEALTHY, RuntimePhase.DEGRADED}
            and runtime.process_identity is not None
        )

    def _repair_terminal_operation(self, receipt: RuntimeActivationReceipt) -> None:
        operation = self._operations.read(receipt.operation_id)
        if operation is None or operation.state not in {
            OperationState.PENDING,
            OperationState.RUNNING,
        }:
            return
        if receipt.classification in {
            RuntimeActivationClassification.HOT_RELOAD,
            RuntimeActivationClassification.RESTART_FALLBACK,
            RuntimeActivationClassification.ACTIVE_BUT_CLIENT_STALE,
            RuntimeActivationClassification.ROLLED_BACK,
        }:
            if receipt.active_identity is None:
                return
            self._journal.complete(
                receipt.receipt_id,
                classification=receipt.classification,
                active_identity=receipt.active_identity,
            )
            return
        if receipt.classification is RuntimeActivationClassification.RELOAD_FAILED:
            self._journal.fail(
                receipt.receipt_id,
                error_code=receipt.error_code or ErrorCode.EFFECT_OUTCOME_UNKNOWN.value,
                error_message=receipt.error_message or "runtime_activation_failed",
                active_identity=receipt.active_identity,
                effect_boundary_crossed=receipt.effect_boundary_crossed,
                retryability=(
                    OperationRetryability.AUTOMATIC
                    if receipt.error_code == ErrorCode.FAILED_BEFORE_EFFECT.value
                    else OperationRetryability.MANUAL
                ),
            )

    def reconcile(
        self,
        *,
        active_runtime: RuntimeRecord | None,
        max_records: int = 2_000,
    ) -> RuntimeActivationReconciliationReport:
        page = self._receipts.list_all(max_records=max_records)
        activated = failed_before = unknown = resumable = terminal = 0
        for envelope in page.records:
            receipt = envelope.value
            if receipt.classification not in {
                RuntimeActivationClassification.ACCEPTED,
                RuntimeActivationClassification.BUILDING,
            }:
                self._repair_terminal_operation(receipt)
                terminal += 1
                continue

            continuation = receipt.continuation_reference
            if (
                continuation is not None
                and _is_durable_operation_reference(continuation)
                and self._operations.read(continuation) is not None
            ):
                resumable += 1

            if self._target_is_active(receipt.target_generation, active_runtime):
                assert active_runtime is not None
                self._journal.complete(
                    receipt.receipt_id,
                    classification=RuntimeActivationClassification.ACTIVE_BUT_CLIENT_STALE,
                    active_identity=self._target_active_identity(
                        receipt.accepted_identity,
                        active_runtime,
                    ),
                )
                activated += 1
                continue

            if not receipt.effect_boundary_crossed:
                self._journal.fail(
                    receipt.receipt_id,
                    error_code=ErrorCode.FAILED_BEFORE_EFFECT.value,
                    error_message="process_restarted_before_activation_effect",
                    effect_boundary_crossed=False,
                    retryability=OperationRetryability.AUTOMATIC,
                )
                failed_before += 1
                continue

            self._journal.fail(
                receipt.receipt_id,
                error_code=ErrorCode.EFFECT_OUTCOME_UNKNOWN.value,
                error_message="process_restarted_without_activation_outcome_evidence",
                active_identity=(
                    self._target_active_identity(receipt.previous_identity, active_runtime)
                    if active_runtime is not None
                    and receipt.previous_identity is not None
                    and active_runtime.active_generation
                    == receipt.previous_identity.config_generation
                    else None
                ),
                effect_boundary_crossed=True,
                retryability=OperationRetryability.MANUAL,
            )
            unknown += 1

        return RuntimeActivationReconciliationReport(
            scanned=len(page.records),
            activated=activated,
            failed_before_effect=failed_before,
            unknown=unknown,
            continuation_resumable=resumable,
            unchanged_terminal=terminal,
            scan_truncated=page.scan_truncated,
        )


@dataclass(frozen=True, slots=True)
class OutcomeReconciliationReport:
    scanned: int
    validated: int
    failed_before_effect: int
    unknown: int
    deferred_active: int
    unchanged_terminal: int
    scan_truncated: bool


class OutcomeReceiptReconciler:
    def __init__(self, ctx: ApplicationContext) -> None:
        self.ctx = ctx

    def reconcile(
        self,
        *,
        max_records: int = 2_000,
        stale_after_seconds: int | None = None,
    ) -> OutcomeReconciliationReport:
        receipts = self.ctx.effect_receipts
        results = self.ctx.operation_result_store
        operations = self.ctx.operation_store
        if receipts is None or results is None or operations is None:
            return OutcomeReconciliationReport(0, 0, 0, 0, 0, 0, False)
        stale_seconds = (
            self.ctx.config.server.idempotency_stale_seconds
            if stale_after_seconds is None
            else stale_after_seconds
        )
        if stale_seconds < 0:
            raise ValueError("stale_after_seconds must be non-negative")

        page = receipts.list_all(max_records=max_records)
        manager = OperationManager(self.ctx)
        now = self.ctx.clock.now_iso()
        now_dt = datetime.fromisoformat(now)
        cutoff = now_dt - timedelta(seconds=stale_seconds)
        validated = failed_before = unknown = deferred = terminal = 0
        for envelope in page.records:
            receipt = envelope.value
            if receipt.state not in {
                EffectReceiptState.ACCEPTED,
                EffectReceiptState.APPLYING,
                EffectReceiptState.APPLIED_UNVALIDATED,
            }:
                terminal += 1
                continue
            task = operations.read(receipt.operation_id)
            if (
                task is not None
                and task.state in {OperationState.PENDING, OperationState.RUNNING}
                and datetime.fromisoformat(task.updated_at) > cutoff
            ):
                deferred += 1
                continue
            result = results.read(receipt.operation_id)
            if result is not None:
                current = receipt
                revision = envelope.revision
                if current.state is not EffectReceiptState.APPLIED_UNVALIDATED:
                    current_envelope = receipts.save(
                        transition_effect_receipt(
                            current,
                            EffectReceiptState.APPLIED_UNVALIDATED,
                            now=now,
                            result_reference=f"operation-result:{receipt.operation_id}",
                            effect_boundary_crossed=current.effect_boundary_crossed,
                        ),
                        expected_revision=revision,
                    )
                    current = current_envelope.value
                    revision = current_envelope.revision
                receipts.save(
                    transition_effect_receipt(
                        current,
                        EffectReceiptState.APPLIED_VALIDATED,
                        now=now,
                        result_reference=f"operation-result:{receipt.operation_id}",
                    ),
                    expected_revision=revision,
                )
                if task is not None and task.state in {
                    OperationState.PENDING,
                    OperationState.RUNNING,
                }:
                    if task.state is OperationState.PENDING:
                        manager.start(receipt.operation_id, now=now)
                    manager.succeed(
                        receipt.operation_id,
                        result_reference=f"operation-result:{receipt.operation_id}",
                        receipt_id=receipt.receipt_id,
                        now=now,
                    )
                validated += 1
                continue
            if receipt.state is EffectReceiptState.ACCEPTED and (
                task is None or task.state is OperationState.PENDING
            ):
                receipts.save(
                    transition_effect_receipt(
                        receipt,
                        EffectReceiptState.FAILED_BEFORE_EFFECT,
                        now=now,
                        error_code=ErrorCode.FAILED_BEFORE_EFFECT.value,
                        error_message="process_restarted_before_effect",
                    ),
                    expected_revision=envelope.revision,
                )
                if task is not None:
                    manager.fail(
                        receipt.operation_id,
                        error_code=ErrorCode.FAILED_BEFORE_EFFECT.value,
                        error_message="process_restarted_before_effect",
                        receipt_id=receipt.receipt_id,
                        retryability=OperationRetryability.AUTOMATIC,
                        now=now,
                    )
                failed_before += 1
                continue
            receipts.save(
                transition_effect_receipt(
                    receipt,
                    EffectReceiptState.UNKNOWN,
                    now=now,
                    error_code=ErrorCode.EFFECT_OUTCOME_UNKNOWN.value,
                    error_message="process_restarted_without_result_evidence",
                    effect_boundary_crossed=receipt.effect_boundary_crossed,
                ),
                expected_revision=envelope.revision,
            )
            if task is not None and task.state in {
                OperationState.PENDING,
                OperationState.RUNNING,
            }:
                try:
                    if task.state is OperationState.PENDING:
                        manager.start(receipt.operation_id, now=now)
                    manager.fail(
                        receipt.operation_id,
                        error_code=ErrorCode.EFFECT_OUTCOME_UNKNOWN.value,
                        error_message="process_restarted_without_result_evidence",
                        receipt_id=receipt.receipt_id,
                        retryability=OperationRetryability.MANUAL,
                        now=now,
                    )
                except RepoForgeError:
                    pass
            unknown += 1

        return OutcomeReconciliationReport(
            scanned=len(page.records),
            validated=validated,
            failed_before_effect=failed_before,
            unknown=unknown,
            deferred_active=deferred,
            unchanged_terminal=terminal,
            scan_truncated=page.scan_truncated,
        )
