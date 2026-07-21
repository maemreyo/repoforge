"""Deterministic startup reconciliation for non-terminal outcome receipts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from ..domain.errors import ErrorCode, RepoForgeError
from ..domain.execution_receipt import EffectReceiptState, transition_effect_receipt
from ..domain.operation_task import OperationRetryability, OperationState
from .context import ApplicationContext
from .operations.manager import OperationManager


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
