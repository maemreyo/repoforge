"""Application orchestration for cross-process idempotent write workflows."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict, dataclass, is_dataclass, replace
from typing import TYPE_CHECKING, Any, TypeVar, cast

from ..domain.errors import ConfigError, ErrorCode, RepoForgeError
from ..domain.execution_receipt import (
    EffectReceiptState,
    create_effect_receipt,
    transition_effect_receipt,
)
from ..domain.operation_task import OperationRetryability
from ..domain.operations import (
    IdempotencyRecord,
    IdempotencyState,
    hash_idempotency_key,
    request_fingerprint,
    unchanged_state_for,
)
from ..domain.redaction import sanitize_persisted_data
from .effect_identity import capture_effect_identity
from .outcome_context import publish_outcome

if TYPE_CHECKING:
    from .context import ApplicationContext

T = TypeVar("T")

_LOCAL_MUTATION_ACTIONS = frozenset(
    {"workspace_write_file", "workspace_edit", "workspace_apply_patch"}
)
_RECONCILE_MISS = object()


@dataclass(slots=True)
class IdempotencyEffectBoundary:
    """Marks the first point after which a failed operation may have changed durable state."""

    started: bool = False
    rolled_back: bool = False
    authoritative_result: Any | None = None

    def begin(self) -> None:
        self.started = True
        self.rolled_back = False

    def rollback(self) -> None:
        if not self.started:
            raise ConfigError("Cannot roll back an effect boundary that was not started")
        self.rolled_back = True

    def record_result(self, result: Any) -> None:
        if not self.started:
            raise ConfigError("Cannot record an authoritative result before the effect boundary")
        self.authoritative_result = result


def _durable_result(value: Any) -> dict[str, Any]:
    raw: Any = asdict(value) if is_dataclass(value) and not isinstance(value, type) else value
    persisted = sanitize_persisted_data(raw)
    if not isinstance(persisted, dict):
        persisted = {"value": persisted}
    json.dumps(persisted, sort_keys=True, allow_nan=False)
    return cast(dict[str, Any], persisted)


def execute_idempotent(
    ctx: ApplicationContext,
    action: str,
    key: str | None,
    request: Any,
    operation: Callable[[], T],
    *,
    details: dict[str, Any] | None = None,
    serialize: Callable[[T], Any] | None = None,
    deserialize: Callable[[Any], T] | None = None,
    effect_boundary: IdempotencyEffectBoundary | None = None,
    reconcile_uncertain: Callable[[], T | None] | None = None,
) -> T:
    """Claim, execute, persist, and replay one reviewed keyed operation."""
    if key is None:
        return ctx.audited(action, details or {}, operation)
    store = ctx.idempotency
    if store is None:
        raise ConfigError("Idempotency storage is not configured")
    operation_result_store = ctx.operation_result_store
    receipt_store = ctx.effect_receipts
    if ctx.operation_store is None or operation_result_store is None or receipt_store is None:
        raise ConfigError("Durable outcome storage is not configured")
    try:
        key_hash = hash_idempotency_key(key)
        fingerprint = request_fingerprint(request)
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc
    correlation = ctx.ids.new_hex(24)
    lock_name = f"idempotency-{action}-{key_hash[:24]}"
    record_details = {
        **(details or {}),
        "idempotency_key_hash": key_hash[:16],
    }
    unchanged = unchanged_state_for(action)

    def publish_record_outcome(record: IdempotencyRecord) -> None:
        if record.receipt_id is None:
            return
        envelope = receipt_store.read(record.receipt_id)
        if envelope is not None:
            publish_outcome(envelope.value)

    def materialize(result: T) -> tuple[Any, T]:
        encoder = serialize or cast(Callable[[T], Any], lambda value: value)
        persisted = sanitize_persisted_data(encoder(result))
        json.dumps(persisted, sort_keys=True, allow_nan=False)
        decoder = deserialize or cast(Callable[[Any], T], lambda value: value)
        return persisted, decoder(persisted)

    def reconcile(record: IdempotencyRecord) -> T | object:
        if reconcile_uncertain is None:
            return _RECONCILE_MISS
        result = reconcile_uncertain()
        if result is None:
            store.delete(action, key_hash)
            return _RECONCILE_MISS
        persisted, safe_result = materialize(result)
        store.save(
            replace(
                record,
                state=IdempotencyState.COMPLETED,
                updated_at=ctx.clock.now_iso(),
                updated_at_epoch=ctx.now_epoch(),
                result=persisted,
            )
        )
        ctx.audit.record(
            action,
            success=True,
            details={
                **record_details,
                "correlation_id": correlation,
                "duration_ms": 0.0,
                "idempotent_replay": True,
                "idempotent_reconciled": True,
            },
        )
        ctx.record_metric(action, success=True, duration_ms=0.0, error_code=None)
        return safe_result

    try:
        with ctx.locks.lock(
            lock_name,
            timeout_seconds=ctx.config.server.idempotency_lock_timeout_seconds,
            metadata={"action": action, "correlation_id": correlation},
        ):
            now_iso = ctx.clock.now_iso()
            now_epoch = ctx.now_epoch()
            existing = store.load(action, key_hash)
            if existing is not None and existing.request_fingerprint != fingerprint:
                raise ConfigError(
                    "IDEMPOTENCY_CONFLICT: the key is already bound to different input",
                    code=ErrorCode.IDEMPOTENCY_CONFLICT,
                    retryable=False,
                    safe_next_action="Use the original reviewed input or choose a new idempotency key.",
                    unchanged_state=unchanged,
                    correlation_id=correlation,
                )
            if existing is not None and existing.state is IdempotencyState.COMPLETED:
                decoder = deserialize or cast(Callable[[Any], T], lambda value: value)
                replayed = decoder(existing.result)
                publish_record_outcome(existing)
                ctx.audit.record(
                    action,
                    success=True,
                    details={
                        **record_details,
                        "correlation_id": correlation,
                        "duration_ms": 0.0,
                        "idempotent_replay": True,
                    },
                )
                ctx.record_metric(action, success=True, duration_ms=0.0, error_code=None)
                return replayed
            if existing is not None and existing.state is IdempotencyState.UNCERTAIN:
                if reconcile_uncertain is not None:
                    reconciled = reconcile(existing)
                    if reconciled is not _RECONCILE_MISS:
                        return cast(T, reconciled)
                    existing = None
                else:
                    raise ConfigError(
                        "IDEMPOTENCY_UNCERTAIN: the mutation may have completed before its result receipt was recorded",
                        code=ErrorCode.IDEMPOTENCY_UNCERTAIN,
                        retryable=False,
                        safe_next_action=(
                            "Inspect the current workspace status and target content, compare it with the "
                            "requested result, and do not retry blindly with a new key."
                        ),
                        unchanged_state=(
                            "The workspace mutation outcome is uncertain and must be inspected explicitly.",
                        ),
                        correlation_id=existing.correlation_id,
                    )
            stale_existing = (
                existing is not None
                and now_epoch - existing.updated_at_epoch
                > ctx.config.server.idempotency_stale_seconds
            )
            if (
                stale_existing
                and existing is not None
                and reconcile_uncertain is not None
                and existing.state is IdempotencyState.IN_PROGRESS
            ):
                reconciled = reconcile(existing)
                if reconciled is not _RECONCILE_MISS:
                    return cast(T, reconciled)
                existing = None
                stale_existing = False
            if (
                stale_existing
                and action in _LOCAL_MUTATION_ACTIONS
                and existing is not None
                and existing.state is IdempotencyState.IN_PROGRESS
            ):
                store.save(
                    replace(
                        existing,
                        state=IdempotencyState.UNCERTAIN,
                        updated_at=now_iso,
                        updated_at_epoch=now_epoch,
                        result=None,
                    )
                )
                raise ConfigError(
                    "IDEMPOTENCY_UNCERTAIN: a stale local mutation claim cannot be replayed safely",
                    code=ErrorCode.IDEMPOTENCY_UNCERTAIN,
                    retryable=False,
                    safe_next_action=(
                        "Inspect the target files and workspace fingerprint before deciding whether "
                        "a new reviewed mutation is required."
                    ),
                    unchanged_state=(
                        "The workspace mutation outcome is uncertain and must be inspected explicitly.",
                    ),
                    correlation_id=existing.correlation_id,
                )
            if existing is not None and not stale_existing:
                raise ConfigError(
                    "IDEMPOTENCY_IN_PROGRESS: the same keyed operation is still running",
                    code=ErrorCode.IDEMPOTENCY_IN_PROGRESS,
                    retryable=True,
                    safe_next_action="Wait for the active operation to finish, then retry with the same key.",
                    unchanged_state=unchanged,
                    correlation_id=existing.correlation_id,
                )
            from .operations.manager import OperationManager

            operations = OperationManager(ctx)
            operation_task = operations.create(
                kind=action,
                phase="accepted",
                cancel_supported=False,
                workspace_id=(
                    str((details or {}).get("workspace_id"))
                    if (details or {}).get("workspace_id") is not None
                    else None
                ),
                now=now_iso,
            )
            receipt_id = f"receipt-{ctx.ids.new_hex(24)}"
            receipt_envelope = receipt_store.create(
                create_effect_receipt(
                    receipt_id=receipt_id,
                    operation_id=operation_task.operation_id,
                    action=action,
                    idempotency_key_hash=key_hash,
                    request_fingerprint=fingerprint,
                    accepted_at=now_iso,
                    correlation_id=correlation,
                    pre_identity=capture_effect_identity(ctx, details),
                )
            )
            claim = IdempotencyRecord(
                action,
                key_hash,
                fingerprint,
                IdempotencyState.IN_PROGRESS,
                now_iso,
                now_epoch,
                correlation,
                receipt_id=receipt_id,
                operation_id=operation_task.operation_id,
            )
            store.save(claim)
            receipt_envelope = receipt_store.save(
                transition_effect_receipt(
                    receipt_envelope.value,
                    EffectReceiptState.APPLYING,
                    now=ctx.clock.now_iso(),
                ),
                expected_revision=receipt_envelope.revision,
            )
            operations.start(operation_task.operation_id, now=ctx.clock.now_iso())

            def execute_and_commit() -> T:
                nonlocal receipt_envelope
                try:
                    result = operation()
                    durable_result = _durable_result(result)
                    result_reference = f"operation-result:{operation_task.operation_id}"
                    operation_result_store.save(operation_task.operation_id, durable_result)
                    receipt_envelope = receipt_store.save(
                        transition_effect_receipt(
                            receipt_envelope.value,
                            EffectReceiptState.APPLIED_UNVALIDATED,
                            now=ctx.clock.now_iso(),
                            result_reference=result_reference,
                            effect_boundary_crossed=(
                                effect_boundary is not None and effect_boundary.started
                            ),
                            post_identity=capture_effect_identity(ctx, details, result=result),
                        ),
                        expected_revision=receipt_envelope.revision,
                    )
                    current = store.load(action, key_hash)
                    if current is None or current.correlation_id != correlation:
                        raise ConfigError(
                            "STALE_IDEMPOTENCY: operation ownership changed before result commit",
                            code=ErrorCode.CONFIG_STALE,
                            retryable=True,
                            unchanged_state=unchanged,
                            correlation_id=correlation,
                        )
                    store.save(
                        replace(
                            current,
                            state=IdempotencyState.COMPLETED,
                            updated_at=ctx.clock.now_iso(),
                            updated_at_epoch=ctx.now_epoch(),
                            result=durable_result,
                        )
                    )
                    try:
                        persisted, safe_result = materialize(result)
                    except Exception as serialization_error:
                        receipt_envelope = receipt_store.save(
                            transition_effect_receipt(
                                receipt_envelope.value,
                                EffectReceiptState.FAILED_AFTER_EFFECT,
                                now=ctx.clock.now_iso(),
                                result_reference=result_reference,
                                error_code=ErrorCode.FAILED_AFTER_EFFECT.value,
                                error_message=type(serialization_error).__name__,
                                post_identity=capture_effect_identity(ctx, details, result=result),
                            ),
                            expected_revision=receipt_envelope.revision,
                        )
                        operations.fail(
                            operation_task.operation_id,
                            error_code=ErrorCode.FAILED_AFTER_EFFECT.value,
                            error_message=type(serialization_error).__name__,
                            result_reference=result_reference,
                            retryability=OperationRetryability.NONE,
                            now=ctx.clock.now_iso(),
                        )
                        raise ConfigError(
                            "FAILED_AFTER_EFFECT: the effect is durable but response serialization failed",
                            code=ErrorCode.FAILED_AFTER_EFFECT,
                            retryable=False,
                            safe_next_action=(
                                "Retry with the same idempotency key to replay the authoritative durable result."
                            ),
                            unchanged_state=(
                                "The effect was applied exactly once and its durable result is available by receipt.",
                            ),
                            correlation_id=correlation,
                            details={
                                "effect_boundary_crossed": True,
                                "operation_id": operation_task.operation_id,
                                "receipt_id": receipt_id,
                                "result_reference": result_reference,
                                "original_error_type": type(serialization_error).__name__,
                            },
                        ) from serialization_error
                    current = store.load(action, key_hash)
                    if current is None or current.correlation_id != correlation:
                        raise ConfigError(
                            "STALE_IDEMPOTENCY: operation ownership changed before validation commit",
                            code=ErrorCode.CONFIG_STALE,
                            retryable=True,
                            correlation_id=correlation,
                        )
                    store.save(
                        replace(
                            current,
                            state=IdempotencyState.COMPLETED,
                            updated_at=ctx.clock.now_iso(),
                            updated_at_epoch=ctx.now_epoch(),
                            result=persisted,
                        )
                    )
                    receipt_envelope = receipt_store.save(
                        transition_effect_receipt(
                            receipt_envelope.value,
                            EffectReceiptState.APPLIED_VALIDATED,
                            now=ctx.clock.now_iso(),
                            result_reference=result_reference,
                        ),
                        expected_revision=receipt_envelope.revision,
                    )
                    operations.succeed(
                        operation_task.operation_id,
                        result_reference=result_reference,
                        now=ctx.clock.now_iso(),
                    )
                    publish_outcome(receipt_envelope.value)
                    return safe_result
                except Exception as exc:
                    if receipt_envelope.value.state is EffectReceiptState.APPLIED_VALIDATED:
                        current = store.load(action, key_hash)
                        if current is not None and current.state is IdempotencyState.COMPLETED:
                            decoder = deserialize or cast(Callable[[Any], T], lambda value: value)
                            publish_record_outcome(current)
                            return decoder(current.result)
                    if (
                        isinstance(exc, RepoForgeError)
                        and exc.code is ErrorCode.FAILED_AFTER_EFFECT
                    ):
                        raise
                    current = store.load(action, key_hash)
                    if (
                        effect_boundary is not None
                        and effect_boundary.authoritative_result is not None
                    ):
                        durable_result = _durable_result(effect_boundary.authoritative_result)
                        result_reference = f"operation-result:{operation_task.operation_id}"
                        operation_result_store.save(operation_task.operation_id, durable_result)
                        receipt_envelope = receipt_store.save(
                            transition_effect_receipt(
                                receipt_envelope.value,
                                EffectReceiptState.FAILED_AFTER_EFFECT,
                                now=ctx.clock.now_iso(),
                                result_reference=result_reference,
                                error_code=ErrorCode.FAILED_AFTER_EFFECT.value,
                                error_message=type(exc).__name__,
                                effect_boundary_crossed=True,
                            ),
                            expected_revision=receipt_envelope.revision,
                        )
                        operations.fail(
                            operation_task.operation_id,
                            error_code=ErrorCode.FAILED_AFTER_EFFECT.value,
                            error_message=type(exc).__name__,
                            result_reference=result_reference,
                            retryability=OperationRetryability.NONE,
                            now=ctx.clock.now_iso(),
                        )
                        if current is not None and current.correlation_id == correlation:
                            store.save(
                                replace(
                                    current,
                                    state=IdempotencyState.COMPLETED,
                                    updated_at=ctx.clock.now_iso(),
                                    updated_at_epoch=ctx.now_epoch(),
                                    result=durable_result,
                                )
                            )
                        raise ConfigError(
                            "FAILED_AFTER_EFFECT: the external effect completed before local finalization failed",
                            code=ErrorCode.FAILED_AFTER_EFFECT,
                            retryable=False,
                            safe_next_action=(
                                "Retry with the same idempotency key to replay the authoritative durable result."
                            ),
                            correlation_id=correlation,
                            details={
                                "effect_boundary_crossed": True,
                                "operation_id": operation_task.operation_id,
                                "receipt_id": receipt_id,
                                "result_reference": result_reference,
                                "original_error_type": type(exc).__name__,
                            },
                        ) from exc
                    rolled_back = effect_boundary is not None and effect_boundary.rolled_back
                    effect_started = receipt_envelope.value.effect_boundary_crossed or (
                        effect_boundary is not None and effect_boundary.started
                    )
                    if current is not None and current.correlation_id == correlation:
                        if rolled_back:
                            receipt_envelope = receipt_store.save(
                                transition_effect_receipt(
                                    receipt_envelope.value,
                                    EffectReceiptState.ROLLED_BACK,
                                    now=ctx.clock.now_iso(),
                                    error_code=ErrorCode.EFFECT_ROLLED_BACK.value,
                                    error_message=type(exc).__name__,
                                    effect_boundary_crossed=True,
                                ),
                                expected_revision=receipt_envelope.revision,
                            )
                            operations.fail(
                                operation_task.operation_id,
                                error_code=ErrorCode.EFFECT_ROLLED_BACK.value,
                                error_message=type(exc).__name__,
                                retryability=OperationRetryability.AUTOMATIC,
                                now=ctx.clock.now_iso(),
                            )
                            store.delete(action, key_hash)
                        elif effect_started:
                            receipt_envelope = receipt_store.save(
                                transition_effect_receipt(
                                    receipt_envelope.value,
                                    EffectReceiptState.UNKNOWN,
                                    now=ctx.clock.now_iso(),
                                    error_code=ErrorCode.EFFECT_OUTCOME_UNKNOWN.value,
                                    error_message=type(exc).__name__,
                                    effect_boundary_crossed=True,
                                ),
                                expected_revision=receipt_envelope.revision,
                            )
                            operations.fail(
                                operation_task.operation_id,
                                error_code=ErrorCode.EFFECT_OUTCOME_UNKNOWN.value,
                                error_message=type(exc).__name__,
                                retryability=OperationRetryability.MANUAL,
                                now=ctx.clock.now_iso(),
                            )
                            store.save(
                                replace(
                                    current,
                                    state=IdempotencyState.UNCERTAIN,
                                    updated_at=ctx.clock.now_iso(),
                                    updated_at_epoch=ctx.now_epoch(),
                                    result=None,
                                )
                            )
                        else:
                            receipt_envelope = receipt_store.save(
                                transition_effect_receipt(
                                    receipt_envelope.value,
                                    EffectReceiptState.FAILED_BEFORE_EFFECT,
                                    now=ctx.clock.now_iso(),
                                    error_code=ErrorCode.FAILED_BEFORE_EFFECT.value,
                                    error_message=type(exc).__name__,
                                ),
                                expected_revision=receipt_envelope.revision,
                            )
                            operations.fail(
                                operation_task.operation_id,
                                error_code=ErrorCode.FAILED_BEFORE_EFFECT.value,
                                error_message=type(exc).__name__,
                                retryability=OperationRetryability.AUTOMATIC,
                                now=ctx.clock.now_iso(),
                            )
                            store.delete(action, key_hash)
                    if rolled_back:
                        raise
                    if effect_started:
                        raise ConfigError(
                            "EFFECT_OUTCOME_UNKNOWN: the effect boundary was crossed without an authoritative result",
                            code=ErrorCode.EFFECT_OUTCOME_UNKNOWN,
                            retryable=False,
                            safe_next_action=(
                                "Inspect the authoritative receipt and target state; do not retry blindly with a new key."
                            ),
                            unchanged_state=(
                                "The effect outcome is unknown and must be reconciled from authoritative state.",
                            ),
                            correlation_id=correlation,
                            details={
                                "effect_boundary_crossed": True,
                                "operation_id": operation_task.operation_id,
                                "receipt_id": receipt_id,
                                "original_error_type": type(exc).__name__,
                            },
                        ) from exc
                    raise

            return ctx.audited(
                action,
                record_details,
                execute_and_commit,
                correlation_id=correlation,
            )
    except RepoForgeError as exc:
        if exc.correlation_id is None:
            exc.correlation_id = correlation
        if not exc.unchanged_state:
            exc.unchanged_state = unchanged
        raise
