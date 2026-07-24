"""Durable runtime-activation identities, receipts, and transition policy."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum
from typing import Any

from .errors import ErrorCode, RepoForgeError
from .redaction import redact_text

RUNTIME_ACTIVATION_SCHEMA_VERSION = 1
_RECEIPT_ID = re.compile(r"^receipt-[a-f0-9]{24}$")
_OPERATION_ID = re.compile(r"^op-[a-f0-9]{24}$")
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_SAFE_PHASE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")


class RuntimeActivationClassification(str, Enum):
    ACCEPTED = "accepted"
    BUILDING = "building"
    HOT_RELOAD = "hot_reload"
    RESTART_FALLBACK = "restart_fallback"
    RELOAD_FAILED = "reload_failed"
    ACTIVE_BUT_CLIENT_STALE = "active_but_client_stale"
    ROLLED_BACK = "rolled_back"


_TERMINAL_SUCCESS = frozenset(
    {
        RuntimeActivationClassification.HOT_RELOAD,
        RuntimeActivationClassification.RESTART_FALLBACK,
        RuntimeActivationClassification.ACTIVE_BUT_CLIENT_STALE,
        RuntimeActivationClassification.ROLLED_BACK,
    }
)
_ALLOWED_TRANSITIONS: dict[
    RuntimeActivationClassification, frozenset[RuntimeActivationClassification]
] = {
    RuntimeActivationClassification.ACCEPTED: frozenset(
        {
            RuntimeActivationClassification.BUILDING,
            RuntimeActivationClassification.ACTIVE_BUT_CLIENT_STALE,
            RuntimeActivationClassification.RELOAD_FAILED,
        }
    ),
    RuntimeActivationClassification.BUILDING: frozenset(
        {
            RuntimeActivationClassification.HOT_RELOAD,
            RuntimeActivationClassification.RESTART_FALLBACK,
            RuntimeActivationClassification.ACTIVE_BUT_CLIENT_STALE,
            RuntimeActivationClassification.ROLLED_BACK,
            RuntimeActivationClassification.RELOAD_FAILED,
        }
    ),
}


@dataclass(frozen=True, slots=True)
class RuntimeActivationIdentity:
    config_generation: int
    source_sha256: str
    resolved_sha256: str
    runtime_active_generation: int | None
    process_identity: str | None
    tool_surface_hash: str | None
    runtime_phase: str


@dataclass(frozen=True, slots=True)
class RuntimeActivationReceipt:
    receipt_id: str
    operation_id: str
    classification: RuntimeActivationClassification
    target_generation: int
    accepted_identity: RuntimeActivationIdentity
    previous_identity: RuntimeActivationIdentity | None
    active_identity: RuntimeActivationIdentity | None
    continuation_reference: str | None
    correlation_id: str
    effect_boundary_crossed: bool
    accepted_at: str
    updated_at: str
    error_code: str | None = None
    error_message: str | None = None
    schema_version: int = RUNTIME_ACTIVATION_SCHEMA_VERSION


def _error(message: str, *, code: ErrorCode = ErrorCode.STATE_INVALID) -> RepoForgeError:
    return RepoForgeError(
        message,
        code=code,
        retryable=False,
        safe_next_action="Inspect the activation operation and receipt before attempting another runtime transition.",
    )


def _timestamp(value: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise _error(f"Runtime activation {field} is not an ISO timestamp") from exc
    if parsed.tzinfo is None:
        raise _error(f"Runtime activation {field} must include a timezone offset")
    return parsed


def _optional_id(value: str | None, field: str) -> str | None:
    if value is None:
        return None
    if _SAFE_ID.fullmatch(value) is None:
        raise _error(f"Runtime activation {field} is invalid")
    return value


def validate_runtime_activation_continuation_reference(value: str | None) -> str | None:
    """Validate an opaque bounded continuation identifier without interpreting it."""

    return _optional_id(value, "continuation reference")


def validate_runtime_activation_identity(
    identity: RuntimeActivationIdentity,
) -> RuntimeActivationIdentity:
    if (
        not isinstance(identity.config_generation, int)
        or isinstance(identity.config_generation, bool)
        or identity.config_generation <= 0
    ):
        raise _error("Runtime activation config generation must be positive")
    for field, value in (
        ("source_sha256", identity.source_sha256),
        ("resolved_sha256", identity.resolved_sha256),
    ):
        if _SHA256.fullmatch(value) is None:
            raise _error(f"Runtime activation {field} must be a lowercase SHA-256")
    if identity.runtime_active_generation is not None and (
        not isinstance(identity.runtime_active_generation, int)
        or isinstance(identity.runtime_active_generation, bool)
        or identity.runtime_active_generation <= 0
    ):
        raise _error("Runtime activation active generation must be positive")
    for field, optional_value in (
        ("process_identity", identity.process_identity),
        ("tool_surface_hash", identity.tool_surface_hash),
    ):
        if optional_value is not None and _SHA256.fullmatch(optional_value) is None:
            raise _error(f"Runtime activation {field} must be a lowercase SHA-256")
    if _SAFE_PHASE.fullmatch(identity.runtime_phase) is None:
        raise _error("Runtime activation phase is invalid")
    if identity.runtime_active_generation is None and identity.process_identity is not None:
        raise _error("Runtime activation process identity requires an active generation")
    return identity


def validate_runtime_activation_receipt(
    receipt: RuntimeActivationReceipt,
) -> RuntimeActivationReceipt:
    if receipt.schema_version != RUNTIME_ACTIVATION_SCHEMA_VERSION:
        raise _error("Runtime activation receipt schema version is unsupported")
    if _RECEIPT_ID.fullmatch(receipt.receipt_id) is None:
        raise _error("Runtime activation receipt id is invalid")
    if _OPERATION_ID.fullmatch(receipt.operation_id) is None:
        raise _error("Runtime activation operation id is invalid")
    accepted = validate_runtime_activation_identity(receipt.accepted_identity)
    previous = (
        validate_runtime_activation_identity(receipt.previous_identity)
        if receipt.previous_identity is not None
        else None
    )
    active = (
        validate_runtime_activation_identity(receipt.active_identity)
        if receipt.active_identity is not None
        else None
    )
    if receipt.target_generation != accepted.config_generation:
        raise _error("Runtime activation target does not match the accepted identity")
    validate_runtime_activation_continuation_reference(receipt.continuation_reference)
    if _SAFE_ID.fullmatch(receipt.correlation_id) is None:
        raise _error("Runtime activation correlation id is invalid")
    accepted_at = _timestamp(receipt.accepted_at, "accepted_at")
    updated_at = _timestamp(receipt.updated_at, "updated_at")
    if updated_at < accepted_at:
        raise _error("Runtime activation updated_at precedes accepted_at")
    if not isinstance(receipt.effect_boundary_crossed, bool):
        raise _error("Runtime activation effect boundary must be a boolean")
    _optional_id(receipt.error_code, "error code")
    if receipt.error_message is not None:
        if not receipt.error_message or len(receipt.error_message) > 2_000:
            raise _error("Runtime activation error message is invalid")
        if receipt.error_message != redact_text(receipt.error_message, limit=2_000):
            raise _error("Runtime activation error message is not safely redacted")
    if receipt.classification in {
        RuntimeActivationClassification.ACCEPTED,
        RuntimeActivationClassification.BUILDING,
    } and (
        active is not None or receipt.error_code is not None or receipt.error_message is not None
    ):
        raise _error("Non-terminal activation receipt contains terminal evidence")
    if receipt.classification in _TERMINAL_SUCCESS and (
        active is None or not receipt.effect_boundary_crossed
    ):
        raise _error("Successful activation receipt requires active identity and effect evidence")
    if receipt.classification in _TERMINAL_SUCCESS and (
        receipt.error_code is not None or receipt.error_message is not None
    ):
        raise _error("Successful activation receipt cannot contain error evidence")
    if (
        receipt.classification is RuntimeActivationClassification.RELOAD_FAILED
        and receipt.error_code is None
    ):
        raise _error("Failed activation receipt requires an error code")
    if (
        receipt.classification
        in {
            RuntimeActivationClassification.HOT_RELOAD,
            RuntimeActivationClassification.RESTART_FALLBACK,
            RuntimeActivationClassification.ACTIVE_BUT_CLIENT_STALE,
        }
        and active is not None
        and active.runtime_active_generation != receipt.target_generation
    ):
        raise _error("Successful activation identity does not match the target generation")
    if receipt.classification is RuntimeActivationClassification.ROLLED_BACK and previous is None:
        raise _error("Rolled-back activation requires a previous identity")
    return receipt


def new_runtime_activation_receipt(
    *,
    receipt_id: str,
    operation_id: str,
    accepted_identity: RuntimeActivationIdentity,
    previous_identity: RuntimeActivationIdentity | None,
    continuation_reference: str | None,
    correlation_id: str,
    accepted_at: str,
) -> RuntimeActivationReceipt:
    receipt = RuntimeActivationReceipt(
        receipt_id=receipt_id,
        operation_id=operation_id,
        classification=RuntimeActivationClassification.ACCEPTED,
        target_generation=accepted_identity.config_generation,
        accepted_identity=accepted_identity,
        previous_identity=previous_identity,
        active_identity=None,
        continuation_reference=validate_runtime_activation_continuation_reference(
            continuation_reference
        ),
        correlation_id=correlation_id,
        effect_boundary_crossed=False,
        accepted_at=accepted_at,
        updated_at=accepted_at,
    )
    return validate_runtime_activation_receipt(receipt)


def transition_runtime_activation_receipt(
    receipt: RuntimeActivationReceipt,
    classification: RuntimeActivationClassification,
    *,
    now: str,
    active_identity: RuntimeActivationIdentity | None = None,
    effect_boundary_crossed: bool | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
) -> RuntimeActivationReceipt:
    validate_runtime_activation_receipt(receipt)
    if (
        classification is not receipt.classification
        and classification not in _ALLOWED_TRANSITIONS.get(receipt.classification, frozenset())
    ):
        raise _error(
            f"Runtime activation transition is not allowed: {receipt.classification.value} -> {classification.value}"
        )
    safe_error = redact_text(error_message, limit=2_000) if error_message is not None else None
    updated = replace(
        receipt,
        classification=classification,
        active_identity=(receipt.active_identity if active_identity is None else active_identity),
        effect_boundary_crossed=(
            receipt.effect_boundary_crossed
            if effect_boundary_crossed is None
            else effect_boundary_crossed
        ),
        error_code=error_code,
        error_message=safe_error,
        updated_at=now,
    )
    return validate_runtime_activation_receipt(updated)


def runtime_activation_identity_payload(
    identity: RuntimeActivationIdentity,
) -> dict[str, object]:
    validate_runtime_activation_identity(identity)
    return {
        "config_generation": identity.config_generation,
        "process_identity": identity.process_identity,
        "resolved_sha256": identity.resolved_sha256,
        "runtime_active_generation": identity.runtime_active_generation,
        "runtime_phase": identity.runtime_phase,
        "source_sha256": identity.source_sha256,
        "tool_surface_hash": identity.tool_surface_hash,
    }


def runtime_activation_identity_from_payload(
    payload: dict[str, Any],
) -> RuntimeActivationIdentity:
    return validate_runtime_activation_identity(
        RuntimeActivationIdentity(
            config_generation=int(payload.get("config_generation", 0)),
            source_sha256=str(payload.get("source_sha256", "")),
            resolved_sha256=str(payload.get("resolved_sha256", "")),
            runtime_active_generation=(
                int(payload["runtime_active_generation"])
                if payload.get("runtime_active_generation") is not None
                else None
            ),
            process_identity=(
                str(payload["process_identity"])
                if payload.get("process_identity") is not None
                else None
            ),
            tool_surface_hash=(
                str(payload["tool_surface_hash"])
                if payload.get("tool_surface_hash") is not None
                else None
            ),
            runtime_phase=str(payload.get("runtime_phase", "")),
        )
    )


def runtime_activation_receipt_payload(
    receipt: RuntimeActivationReceipt,
) -> dict[str, object]:
    validate_runtime_activation_receipt(receipt)
    return {
        "accepted_at": receipt.accepted_at,
        "accepted_identity": runtime_activation_identity_payload(receipt.accepted_identity),
        "active_identity": (
            runtime_activation_identity_payload(receipt.active_identity)
            if receipt.active_identity is not None
            else None
        ),
        "classification": receipt.classification.value,
        "continuation_reference": receipt.continuation_reference,
        "correlation_id": receipt.correlation_id,
        "effect_boundary_crossed": receipt.effect_boundary_crossed,
        "error_code": receipt.error_code,
        "error_message": receipt.error_message,
        "operation_id": receipt.operation_id,
        "previous_identity": (
            runtime_activation_identity_payload(receipt.previous_identity)
            if receipt.previous_identity is not None
            else None
        ),
        "receipt_id": receipt.receipt_id,
        "schema_version": receipt.schema_version,
        "target_generation": receipt.target_generation,
        "updated_at": receipt.updated_at,
    }


def runtime_activation_receipt_from_payload(
    payload: dict[str, Any],
) -> RuntimeActivationReceipt:
    accepted_raw = payload.get("accepted_identity")
    previous_raw = payload.get("previous_identity")
    active_raw = payload.get("active_identity")
    if not isinstance(accepted_raw, dict):
        raise _error("Runtime activation accepted identity payload is invalid")
    if previous_raw is not None and not isinstance(previous_raw, dict):
        raise _error("Runtime activation previous identity payload is invalid")
    if active_raw is not None and not isinstance(active_raw, dict):
        raise _error("Runtime activation active identity payload is invalid")
    try:
        classification = RuntimeActivationClassification(str(payload.get("classification", "")))
    except ValueError as exc:
        raise _error("Runtime activation classification payload is invalid") from exc
    return validate_runtime_activation_receipt(
        RuntimeActivationReceipt(
            receipt_id=str(payload.get("receipt_id", "")),
            operation_id=str(payload.get("operation_id", "")),
            classification=classification,
            target_generation=int(payload.get("target_generation", 0)),
            accepted_identity=runtime_activation_identity_from_payload(accepted_raw),
            previous_identity=(
                runtime_activation_identity_from_payload(previous_raw)
                if previous_raw is not None
                else None
            ),
            active_identity=(
                runtime_activation_identity_from_payload(active_raw)
                if active_raw is not None
                else None
            ),
            continuation_reference=(
                str(payload["continuation_reference"])
                if payload.get("continuation_reference") is not None
                else None
            ),
            correlation_id=str(payload.get("correlation_id", "")),
            effect_boundary_crossed=bool(payload.get("effect_boundary_crossed", False)),
            accepted_at=str(payload.get("accepted_at", "")),
            updated_at=str(payload.get("updated_at", "")),
            error_code=(
                str(payload["error_code"]) if payload.get("error_code") is not None else None
            ),
            error_message=(
                str(payload["error_message"]) if payload.get("error_message") is not None else None
            ),
            schema_version=int(payload.get("schema_version", 0)),
        )
    )


__all__ = [
    "RUNTIME_ACTIVATION_SCHEMA_VERSION",
    "RuntimeActivationClassification",
    "RuntimeActivationIdentity",
    "RuntimeActivationReceipt",
    "new_runtime_activation_receipt",
    "runtime_activation_receipt_from_payload",
    "runtime_activation_receipt_payload",
    "transition_runtime_activation_receipt",
    "validate_runtime_activation_continuation_reference",
    "validate_runtime_activation_identity",
    "validate_runtime_activation_receipt",
]
