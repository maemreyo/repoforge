"""Shared durable outcome orchestration for non-keyed state-changing operations."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict, is_dataclass
from typing import Any, TypeVar, cast

from ..domain.errors import ConfigError, ErrorCode, RepoForgeError
from ..domain.execution_receipt import (
    EffectReceiptState,
    create_effect_receipt,
    transition_effect_receipt,
)
from ..domain.operation_task import OperationRetryability
from ..domain.operations import request_fingerprint
from ..domain.redaction import sanitize_persisted_data
from .context import ApplicationContext
from .effect_identity import capture_effect_identity
from .idempotency import IdempotencyEffectBoundary
from .operations.manager import OperationManager
from .outcome_context import publish_outcome

T = TypeVar("T")


def _durable_result(value: Any) -> dict[str, Any]:
    raw: Any = asdict(value) if is_dataclass(value) and not isinstance(value, type) else value
    persisted = sanitize_persisted_data(raw)
    if not isinstance(persisted, dict):
        persisted = {"value": persisted}
    json.dumps(persisted, sort_keys=True, allow_nan=False)
    return cast(dict[str, Any], persisted)


def execute_with_outcome_receipt(
    ctx: ApplicationContext,
    action: str,
    request: Any,
    operation: Callable[[], T],
    *,
    details: dict[str, Any] | None = None,
    serialize: Callable[[T], Any] | None = None,
    effect_boundary: IdempotencyEffectBoundary,
    deferred_exceptions: tuple[type[BaseException], ...] = (),
) -> T:
    """Execute one non-keyed mutation behind an authoritative durable receipt."""

    operation_result_store = ctx.operation_result_store
    receipt_store = ctx.effect_receipts
    if ctx.operation_store is None or operation_result_store is None or receipt_store is None:
        raise ConfigError("Durable outcome storage is not configured")
    try:
        fingerprint = request_fingerprint(request)
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc

    correlation = ctx.ids.new_hex(24)
    now = ctx.clock.now_iso()
    manager = OperationManager(ctx)
    task = manager.create(
        kind=action,
        phase="accepted",
        cancel_supported=False,
        workspace_id=(
            str((details or {}).get("workspace_id"))
            if (details or {}).get("workspace_id") is not None
            else None
        ),
        now=now,
    )
    receipt_id = f"receipt-{ctx.ids.new_hex(24)}"
    receipt = receipt_store.create(
        create_effect_receipt(
            receipt_id=receipt_id,
            operation_id=task.operation_id,
            action=action,
            idempotency_key_hash=None,
            request_fingerprint=fingerprint,
            accepted_at=now,
            correlation_id=correlation,
            pre_identity=capture_effect_identity(ctx, details),
        )
    )
    receipt = receipt_store.save(
        transition_effect_receipt(
            receipt.value,
            EffectReceiptState.APPLYING,
            now=ctx.clock.now_iso(),
        ),
        expected_revision=receipt.revision,
    )
    manager.start(task.operation_id, now=ctx.clock.now_iso())
    result: T | None = None

    def execute() -> T:
        nonlocal receipt, result
        result = operation()
        durable = _durable_result(result)
        result_reference = f"operation-result:{task.operation_id}"
        operation_result_store.save(task.operation_id, durable)
        receipt = receipt_store.save(
            transition_effect_receipt(
                receipt.value,
                EffectReceiptState.APPLIED_UNVALIDATED,
                now=ctx.clock.now_iso(),
                result_reference=result_reference,
                effect_boundary_crossed=effect_boundary.started,
                post_identity=capture_effect_identity(ctx, details, result=result),
            ),
            expected_revision=receipt.revision,
        )
        encoder = serialize or cast(Callable[[T], Any], lambda value: value)
        try:
            validated = sanitize_persisted_data(encoder(result))
            json.dumps(validated, sort_keys=True, allow_nan=False)
        except Exception as exc:
            receipt = receipt_store.save(
                transition_effect_receipt(
                    receipt.value,
                    EffectReceiptState.FAILED_AFTER_EFFECT,
                    now=ctx.clock.now_iso(),
                    result_reference=result_reference,
                    error_code=ErrorCode.FAILED_AFTER_EFFECT.value,
                    error_message=type(exc).__name__,
                ),
                expected_revision=receipt.revision,
            )
            manager.fail(
                task.operation_id,
                error_code=ErrorCode.FAILED_AFTER_EFFECT.value,
                error_message=type(exc).__name__,
                result_reference=result_reference,
                retryability=OperationRetryability.NONE,
                now=ctx.clock.now_iso(),
            )
            raise ConfigError(
                "FAILED_AFTER_EFFECT: the effect is durable but output validation failed",
                code=ErrorCode.FAILED_AFTER_EFFECT,
                retryable=False,
                safe_next_action=(
                    "Read the authoritative operation result using the returned operation and receipt identifiers."
                ),
                correlation_id=correlation,
                details={
                    "effect_boundary_crossed": True,
                    "operation_id": task.operation_id,
                    "receipt_id": receipt_id,
                    "result_reference": result_reference,
                    "original_error_type": type(exc).__name__,
                },
            ) from exc
        receipt = receipt_store.save(
            transition_effect_receipt(
                receipt.value,
                EffectReceiptState.APPLIED_VALIDATED,
                now=ctx.clock.now_iso(),
                result_reference=result_reference,
            ),
            expected_revision=receipt.revision,
        )
        manager.succeed(
            task.operation_id,
            result_reference=result_reference,
            now=ctx.clock.now_iso(),
        )
        publish_outcome(receipt.value)
        return result

    try:
        return ctx.audited(
            action,
            details or {},
            execute,
            correlation_id=correlation,
            mutating=True,
        )
    except Exception as exc:
        if receipt.value.state is EffectReceiptState.APPLIED_VALIDATED and result is not None:
            publish_outcome(receipt.value)
            return result
        if isinstance(exc, RepoForgeError) and exc.code is ErrorCode.FAILED_AFTER_EFFECT:
            raise
        if isinstance(exc, deferred_exceptions):
            # Journal-backed operations own crash recovery and can resolve the
            # exact outcome on the next same-key invocation. Preserve the raw
            # crash signal and leave this receipt non-terminal until that
            # stronger evidence or the startup stale-lease reconciler acts.
            raise
        if effect_boundary.authoritative_result is not None:
            durable = _durable_result(effect_boundary.authoritative_result)
            result_reference = f"operation-result:{task.operation_id}"
            operation_result_store.save(task.operation_id, durable)
            receipt = receipt_store.save(
                transition_effect_receipt(
                    receipt.value,
                    EffectReceiptState.FAILED_AFTER_EFFECT,
                    now=ctx.clock.now_iso(),
                    result_reference=result_reference,
                    error_code=ErrorCode.FAILED_AFTER_EFFECT.value,
                    error_message=type(exc).__name__,
                    effect_boundary_crossed=True,
                    post_identity=capture_effect_identity(
                        ctx,
                        details,
                        result=effect_boundary.authoritative_result,
                    ),
                ),
                expected_revision=receipt.revision,
            )
            manager.fail(
                task.operation_id,
                error_code=ErrorCode.FAILED_AFTER_EFFECT.value,
                error_message=type(exc).__name__,
                result_reference=result_reference,
                retryability=OperationRetryability.NONE,
                now=ctx.clock.now_iso(),
            )
            raise ConfigError(
                "FAILED_AFTER_EFFECT: the effect completed before local finalization failed",
                code=ErrorCode.FAILED_AFTER_EFFECT,
                retryable=False,
                safe_next_action=(
                    "Read the authoritative operation result using the returned operation and receipt identifiers."
                ),
                correlation_id=correlation,
                details={
                    "effect_boundary_crossed": True,
                    "operation_id": task.operation_id,
                    "receipt_id": receipt_id,
                    "result_reference": result_reference,
                    "original_error_type": type(exc).__name__,
                },
            ) from exc
        rolled_back = effect_boundary.rolled_back
        effect_started = receipt.value.effect_boundary_crossed or effect_boundary.started
        if rolled_back:
            receipt = receipt_store.save(
                transition_effect_receipt(
                    receipt.value,
                    EffectReceiptState.ROLLED_BACK,
                    now=ctx.clock.now_iso(),
                    error_code=ErrorCode.EFFECT_ROLLED_BACK.value,
                    error_message=type(exc).__name__,
                    effect_boundary_crossed=True,
                    post_identity=capture_effect_identity(ctx, details),
                ),
                expected_revision=receipt.revision,
            )
            manager.fail(
                task.operation_id,
                error_code=ErrorCode.EFFECT_ROLLED_BACK.value,
                error_message=type(exc).__name__,
                retryability=OperationRetryability.AUTOMATIC,
                now=ctx.clock.now_iso(),
            )
            raise
        if effect_started:
            receipt = receipt_store.save(
                transition_effect_receipt(
                    receipt.value,
                    EffectReceiptState.UNKNOWN,
                    now=ctx.clock.now_iso(),
                    error_code=ErrorCode.EFFECT_OUTCOME_UNKNOWN.value,
                    error_message=type(exc).__name__,
                    effect_boundary_crossed=True,
                    post_identity=capture_effect_identity(ctx, details),
                ),
                expected_revision=receipt.revision,
            )
            manager.fail(
                task.operation_id,
                error_code=ErrorCode.EFFECT_OUTCOME_UNKNOWN.value,
                error_message=type(exc).__name__,
                retryability=OperationRetryability.MANUAL,
                now=ctx.clock.now_iso(),
            )
            raise ConfigError(
                "EFFECT_OUTCOME_UNKNOWN: the effect boundary was crossed without an authoritative result",
                code=ErrorCode.EFFECT_OUTCOME_UNKNOWN,
                retryable=False,
                safe_next_action=(
                    "Inspect the authoritative receipt and target state; do not retry the operation blindly."
                ),
                correlation_id=correlation,
                details={
                    "effect_boundary_crossed": True,
                    "operation_id": task.operation_id,
                    "receipt_id": receipt_id,
                    "original_error_type": type(exc).__name__,
                },
            ) from exc
        receipt_store.save(
            transition_effect_receipt(
                receipt.value,
                EffectReceiptState.FAILED_BEFORE_EFFECT,
                now=ctx.clock.now_iso(),
                error_code=ErrorCode.FAILED_BEFORE_EFFECT.value,
                error_message=type(exc).__name__,
            ),
            expected_revision=receipt.revision,
        )
        manager.fail(
            task.operation_id,
            error_code=ErrorCode.FAILED_BEFORE_EFFECT.value,
            error_message=type(exc).__name__,
            retryability=OperationRetryability.AUTOMATIC,
            now=ctx.clock.now_iso(),
        )
        raise
