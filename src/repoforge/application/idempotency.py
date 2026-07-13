"""Application orchestration for cross-process idempotent write workflows."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from typing import TYPE_CHECKING, Any, TypeVar, cast

from ..domain.errors import ConfigError, ErrorCode, RepoForgeError
from ..domain.operations import (
    IdempotencyRecord,
    IdempotencyState,
    hash_idempotency_key,
    request_fingerprint,
    unchanged_state_for,
)
from ..domain.redaction import sanitize_persisted_data

if TYPE_CHECKING:
    from .context import ApplicationContext

T = TypeVar("T")


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
) -> T:
    """Claim, execute, persist, and replay one reviewed keyed operation."""
    if key is None:
        return ctx.audited(action, details or {}, operation)
    store = ctx.idempotency
    if store is None:
        raise ConfigError("Idempotency storage is not configured")
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
            if (
                existing is not None
                and now_epoch - existing.updated_at_epoch
                <= ctx.config.server.idempotency_stale_seconds
            ):
                raise ConfigError(
                    "IDEMPOTENCY_IN_PROGRESS: the same keyed operation is still running",
                    code=ErrorCode.IDEMPOTENCY_IN_PROGRESS,
                    retryable=True,
                    safe_next_action="Wait for the active operation to finish, then retry with the same key.",
                    unchanged_state=unchanged,
                    correlation_id=existing.correlation_id,
                )
            claim = IdempotencyRecord(
                action,
                key_hash,
                fingerprint,
                IdempotencyState.IN_PROGRESS,
                now_iso,
                now_epoch,
                correlation,
            )
            store.save(claim)

            def execute_and_commit() -> T:
                try:
                    result = operation()
                    encoder = serialize or cast(Callable[[T], Any], lambda value: value)
                    persisted = sanitize_persisted_data(encoder(result))
                    json.dumps(persisted, sort_keys=True, allow_nan=False)
                    decoder = deserialize or cast(Callable[[Any], T], lambda value: value)
                    safe_result = decoder(persisted)
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
                            result=persisted,
                        )
                    )
                    return safe_result
                except Exception:
                    current = store.load(action, key_hash)
                    if current is not None and current.correlation_id == correlation:
                        store.delete(action, key_hash)
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
