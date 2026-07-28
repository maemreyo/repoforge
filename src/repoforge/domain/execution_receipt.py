"""Immutable, bounded receipts for one accepted-plan stage execution."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum
from typing import Any, NoReturn

from .errors import ErrorCode, RepoForgeError

EXECUTION_RECEIPT_SCHEMA_VERSION = 2
EFFECT_RECEIPT_SCHEMA_VERSION = 1
_RECEIPT_ID = re.compile(r"^receipt-[0-9a-f]{24}$")
_OPERATION_ID = re.compile(r"^op-[0-9a-f]{24}$")
_PLAN_ID = re.compile(r"^plan-[0-9a-f]{24}$")
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SHA40 = re.compile(r"^[0-9a-f]{40}$")
_SHA64 = re.compile(r"^[0-9a-f]{64}$")


class StageReceiptStatus(str, Enum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"


class StageCacheStatus(str, Enum):
    NOT_CACHEABLE = "not_cacheable"
    MISS = "miss"
    HIT = "hit"


class EffectReceiptState(str, Enum):
    ACCEPTED = "accepted"
    APPLYING = "applying"
    APPLIED_UNVALIDATED = "applied_unvalidated"
    APPLIED_VALIDATED = "applied_validated"
    ROLLED_BACK = "rolled_back"
    FAILED_BEFORE_EFFECT = "failed_before_effect"
    FAILED_AFTER_EFFECT = "failed_after_effect"
    UNKNOWN = "unknown"


_EFFECT_TRANSITIONS: dict[EffectReceiptState, frozenset[EffectReceiptState]] = {
    EffectReceiptState.ACCEPTED: frozenset(
        {
            EffectReceiptState.APPLYING,
            EffectReceiptState.FAILED_BEFORE_EFFECT,
            EffectReceiptState.UNKNOWN,
        }
    ),
    EffectReceiptState.APPLYING: frozenset(
        {
            EffectReceiptState.APPLIED_UNVALIDATED,
            EffectReceiptState.ROLLED_BACK,
            EffectReceiptState.FAILED_BEFORE_EFFECT,
            EffectReceiptState.FAILED_AFTER_EFFECT,
            EffectReceiptState.UNKNOWN,
        }
    ),
    EffectReceiptState.APPLIED_UNVALIDATED: frozenset(
        {
            EffectReceiptState.APPLIED_VALIDATED,
            EffectReceiptState.FAILED_AFTER_EFFECT,
            EffectReceiptState.ROLLED_BACK,
            EffectReceiptState.UNKNOWN,
        }
    ),
}

EffectIdentity = tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class EffectReceipt:
    receipt_id: str
    operation_id: str
    action: str
    idempotency_key_hash: str | None
    request_fingerprint: str
    state: EffectReceiptState
    accepted_at: str
    updated_at: str
    correlation_id: str
    pre_identity: EffectIdentity = ()
    post_identity: EffectIdentity = ()
    effect_boundary_crossed: bool = False
    result_reference: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    schema_version: int = EFFECT_RECEIPT_SCHEMA_VERSION


@dataclass(frozen=True, slots=True)
class WorkspaceIdentity:
    head_sha: str
    workspace_fingerprint: str
    config_generation: str
    policy_hash: str

    def __post_init__(self) -> None:
        if _SHA40.fullmatch(self.head_sha) is None:
            _invalid("Stage receipt HEAD SHA is invalid")
        for name, value in (
            ("workspace fingerprint", self.workspace_fingerprint),
            ("configuration generation", self.config_generation),
            ("policy hash", self.policy_hash),
        ):
            if _SHA64.fullmatch(value) is None:
                _invalid(f"Stage receipt {name} is invalid")

    def payload(self) -> dict[str, str]:
        return {
            "config_generation": self.config_generation,
            "head_sha": self.head_sha,
            "policy_hash": self.policy_hash,
            "workspace_fingerprint": self.workspace_fingerprint,
        }


@dataclass(frozen=True, slots=True)
class ArtifactDigest:
    path: str
    sha256: str

    def __post_init__(self) -> None:
        if (
            not self.path
            or len(self.path) > 512
            or self.path.startswith("/")
            or ".." in self.path.split("/")
            or any(ord(character) < 32 for character in self.path)
        ):
            _invalid("Stage receipt artifact path is unsafe")
        if _SHA64.fullmatch(self.sha256) is None:
            _invalid("Stage receipt artifact digest is invalid")

    def payload(self) -> dict[str, str]:
        return {"path": self.path, "sha256": self.sha256}


@dataclass(frozen=True, slots=True)
class StageReceipt:
    receipt_id: str
    operation_id: str
    ordinal: int
    plan_id: str
    plan_hash: str
    workspace_id: str
    stage_id: str
    kind: str
    target: str
    boundary: str
    started_at: str
    finished_at: str
    pre_identity: WorkspaceIdentity
    post_identity: WorkspaceIdentity
    target_identity: str
    environment_identity_schema_version: int
    environment_identity: str
    requested_policy_hash: str
    effective_policy_hash: str
    status: StageReceiptStatus
    failure_class: str | None
    result_reference: str | None
    artifact_digests: tuple[ArtifactDigest, ...]
    cache_status: StageCacheStatus
    source_changed: bool
    schema_version: int = EXECUTION_RECEIPT_SCHEMA_VERSION


def _invalid(message: str) -> NoReturn:
    raise RepoForgeError(
        message,
        code=ErrorCode.STATE_INVALID,
        safe_next_action="Inspect the accepted plan and durable stage evidence before retrying.",
    )


def _timestamp(value: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise RepoForgeError(
            f"Stage receipt {field} is not an ISO timestamp",
            code=ErrorCode.STATE_INVALID,
        ) from exc
    if parsed.tzinfo is None:
        _invalid(f"Stage receipt {field} must include a timezone offset")
    return parsed


def _digest(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()


def _safe_optional(value: str | None, field: str, *, limit: int = 256) -> str | None:
    if value is None:
        return None
    if not value or len(value) > limit or any(ord(character) < 32 for character in value):
        _invalid(f"Stage receipt {field} is invalid")
    return value


def _semantic_payload(
    *,
    operation_id: str,
    ordinal: int,
    plan_id: str,
    plan_hash: str,
    workspace_id: str,
    stage_id: str,
    kind: str,
    target: str,
    boundary: str,
    started_at: str,
    finished_at: str,
    pre_identity: WorkspaceIdentity,
    post_identity: WorkspaceIdentity,
    target_identity: str,
    environment_identity_schema_version: int,
    environment_identity: str,
    requested_policy_hash: str,
    effective_policy_hash: str,
    status: StageReceiptStatus,
    failure_class: str | None,
    result_reference: str | None,
    artifact_digests: tuple[ArtifactDigest, ...],
    cache_status: StageCacheStatus,
    source_changed: bool,
) -> dict[str, object]:
    return {
        "artifact_digests": [artifact.payload() for artifact in artifact_digests],
        "boundary": boundary,
        "cache_status": cache_status.value,
        "effective_policy_hash": effective_policy_hash,
        "environment_identity": environment_identity,
        "environment_identity_schema_version": environment_identity_schema_version,
        "failure_class": failure_class,
        "finished_at": finished_at,
        "kind": kind,
        "operation_id": operation_id,
        "ordinal": ordinal,
        "plan_hash": plan_hash,
        "plan_id": plan_id,
        "post_identity": post_identity.payload(),
        "pre_identity": pre_identity.payload(),
        "requested_policy_hash": requested_policy_hash,
        "result_reference": result_reference,
        "source_changed": source_changed,
        "stage_id": stage_id,
        "started_at": started_at,
        "status": status.value,
        "target": target,
        "target_identity": target_identity,
        "workspace_id": workspace_id,
    }


def create_stage_receipt(
    *,
    operation_id: str,
    ordinal: int,
    plan_id: str,
    plan_hash: str,
    workspace_id: str,
    stage_id: str,
    kind: str,
    target: str,
    boundary: str,
    started_at: str,
    finished_at: str,
    pre_identity: WorkspaceIdentity,
    post_identity: WorkspaceIdentity,
    target_identity: str,
    environment_identity_schema_version: int,
    environment_identity: str,
    requested_policy_hash: str,
    effective_policy_hash: str,
    status: StageReceiptStatus,
    failure_class: str | None = None,
    result_reference: str | None = None,
    artifact_digests: tuple[ArtifactDigest, ...] = (),
    cache_status: StageCacheStatus = StageCacheStatus.NOT_CACHEABLE,
    source_changed: bool = False,
) -> StageReceipt:
    if _OPERATION_ID.fullmatch(operation_id) is None:
        _invalid("Stage receipt operation id is invalid")
    if _PLAN_ID.fullmatch(plan_id) is None or _SHA64.fullmatch(plan_hash) is None:
        _invalid("Stage receipt plan identity is invalid")
    if not isinstance(ordinal, int) or isinstance(ordinal, bool) or not 0 <= ordinal <= 127:
        _invalid("Stage receipt ordinal is invalid")
    for field, value in (
        ("workspace_id", workspace_id),
        ("stage_id", stage_id),
        ("kind", kind),
        ("target", target),
        ("boundary", boundary),
    ):
        if _SAFE_ID.fullmatch(value) is None:
            _invalid(f"Stage receipt {field} is invalid")
    if _SHA64.fullmatch(target_identity) is None:
        _invalid("Stage receipt target identity is invalid")
    if (
        not isinstance(environment_identity_schema_version, int)
        or isinstance(environment_identity_schema_version, bool)
        or environment_identity_schema_version < 1
    ):
        _invalid("Stage receipt environment identity schema version is invalid")
    for field, value in (
        ("environment identity", environment_identity),
        ("requested policy hash", requested_policy_hash),
        ("effective policy hash", effective_policy_hash),
    ):
        if _SHA64.fullmatch(value) is None:
            _invalid(f"Stage receipt {field} is invalid")
    _safe_optional(failure_class, "failure_class", limit=128)
    _safe_optional(result_reference, "result_reference", limit=256)
    if len(artifact_digests) > 64 or len({item.path for item in artifact_digests}) != len(
        artifact_digests
    ):
        _invalid("Stage receipt artifacts must be unique and bounded")
    started = _timestamp(started_at, "started_at")
    finished = _timestamp(finished_at, "finished_at")
    if finished < started:
        _invalid("Stage receipt finished_at cannot precede started_at")
    if status is StageReceiptStatus.SUCCEEDED and failure_class is not None:
        _invalid("Successful stage receipt cannot carry a failure class")
    if status is StageReceiptStatus.FAILED and failure_class is None:
        _invalid("Failed stage receipt requires a failure class")
    semantic = _semantic_payload(
        operation_id=operation_id,
        ordinal=ordinal,
        plan_id=plan_id,
        plan_hash=plan_hash,
        workspace_id=workspace_id,
        stage_id=stage_id,
        kind=kind,
        target=target,
        boundary=boundary,
        started_at=started_at,
        finished_at=finished_at,
        pre_identity=pre_identity,
        post_identity=post_identity,
        target_identity=target_identity,
        environment_identity_schema_version=environment_identity_schema_version,
        environment_identity=environment_identity,
        requested_policy_hash=requested_policy_hash,
        effective_policy_hash=effective_policy_hash,
        status=status,
        failure_class=failure_class,
        result_reference=result_reference,
        artifact_digests=artifact_digests,
        cache_status=cache_status,
        source_changed=source_changed,
    )
    receipt_id = f"receipt-{_digest(semantic)[:24]}"
    return StageReceipt(
        receipt_id=receipt_id,
        operation_id=operation_id,
        ordinal=ordinal,
        plan_id=plan_id,
        plan_hash=plan_hash,
        workspace_id=workspace_id,
        stage_id=stage_id,
        kind=kind,
        target=target,
        boundary=boundary,
        started_at=started_at,
        finished_at=finished_at,
        pre_identity=pre_identity,
        post_identity=post_identity,
        target_identity=target_identity,
        environment_identity_schema_version=environment_identity_schema_version,
        environment_identity=environment_identity,
        requested_policy_hash=requested_policy_hash,
        effective_policy_hash=effective_policy_hash,
        status=status,
        failure_class=failure_class,
        result_reference=result_reference,
        artifact_digests=artifact_digests,
        cache_status=cache_status,
        source_changed=source_changed,
    )


def receipt_payload(receipt: StageReceipt) -> dict[str, object]:
    return {
        "receipt_id": receipt.receipt_id,
        "schema_version": receipt.schema_version,
        **_semantic_payload(
            operation_id=receipt.operation_id,
            ordinal=receipt.ordinal,
            plan_id=receipt.plan_id,
            plan_hash=receipt.plan_hash,
            workspace_id=receipt.workspace_id,
            stage_id=receipt.stage_id,
            kind=receipt.kind,
            target=receipt.target,
            boundary=receipt.boundary,
            started_at=receipt.started_at,
            finished_at=receipt.finished_at,
            pre_identity=receipt.pre_identity,
            post_identity=receipt.post_identity,
            target_identity=receipt.target_identity,
            environment_identity_schema_version=receipt.environment_identity_schema_version,
            environment_identity=receipt.environment_identity,
            requested_policy_hash=receipt.requested_policy_hash,
            effective_policy_hash=receipt.effective_policy_hash,
            status=receipt.status,
            failure_class=receipt.failure_class,
            result_reference=receipt.result_reference,
            artifact_digests=receipt.artifact_digests,
            cache_status=receipt.cache_status,
            source_changed=receipt.source_changed,
        ),
    }


def validate_stage_receipt(receipt: StageReceipt) -> StageReceipt:
    if receipt.schema_version != EXECUTION_RECEIPT_SCHEMA_VERSION:
        _invalid("Stage receipt schema version is unsupported")
    expected = create_stage_receipt(
        operation_id=receipt.operation_id,
        ordinal=receipt.ordinal,
        plan_id=receipt.plan_id,
        plan_hash=receipt.plan_hash,
        workspace_id=receipt.workspace_id,
        stage_id=receipt.stage_id,
        kind=receipt.kind,
        target=receipt.target,
        boundary=receipt.boundary,
        started_at=receipt.started_at,
        finished_at=receipt.finished_at,
        pre_identity=receipt.pre_identity,
        post_identity=receipt.post_identity,
        target_identity=receipt.target_identity,
        environment_identity_schema_version=receipt.environment_identity_schema_version,
        environment_identity=receipt.environment_identity,
        requested_policy_hash=receipt.requested_policy_hash,
        effective_policy_hash=receipt.effective_policy_hash,
        status=receipt.status,
        failure_class=receipt.failure_class,
        result_reference=receipt.result_reference,
        artifact_digests=receipt.artifact_digests,
        cache_status=receipt.cache_status,
        source_changed=receipt.source_changed,
    )
    if (
        receipt.receipt_id != expected.receipt_id
        or _RECEIPT_ID.fullmatch(receipt.receipt_id) is None
    ):
        _invalid("Stage receipt id does not match its content")
    return receipt


def stage_receipt_from_payload(payload: dict[str, Any]) -> StageReceipt:
    def identity(name: str) -> WorkspaceIdentity:
        raw = payload.get(name)
        if not isinstance(raw, dict):
            _invalid(f"Stage receipt {name} is missing")
        return WorkspaceIdentity(
            head_sha=str(raw.get("head_sha", "")),
            workspace_fingerprint=str(raw.get("workspace_fingerprint", "")),
            config_generation=str(raw.get("config_generation", "")),
            policy_hash=str(raw.get("policy_hash", "")),
        )

    raw_artifacts = payload.get("artifact_digests", [])
    if not isinstance(raw_artifacts, list):
        _invalid("Stage receipt artifacts payload is invalid")
    artifacts: list[ArtifactDigest] = []
    for raw in raw_artifacts:
        if not isinstance(raw, dict):
            _invalid("Stage receipt artifact payload is invalid")
        artifacts.append(
            ArtifactDigest(path=str(raw.get("path", "")), sha256=str(raw.get("sha256", "")))
        )
    ordinal = payload.get("ordinal")
    schema_version = payload.get("schema_version")
    if not isinstance(ordinal, int) or isinstance(ordinal, bool):
        _invalid("Stage receipt ordinal payload is invalid")
    if not isinstance(schema_version, int) or isinstance(schema_version, bool):
        _invalid("Stage receipt schema payload is invalid")
    identity_schema_version = payload.get("environment_identity_schema_version")
    if not isinstance(identity_schema_version, int) or isinstance(identity_schema_version, bool):
        _invalid("Stage receipt environment identity schema payload is invalid")
    receipt = StageReceipt(
        receipt_id=str(payload.get("receipt_id", "")),
        operation_id=str(payload.get("operation_id", "")),
        ordinal=ordinal,
        plan_id=str(payload.get("plan_id", "")),
        plan_hash=str(payload.get("plan_hash", "")),
        workspace_id=str(payload.get("workspace_id", "")),
        stage_id=str(payload.get("stage_id", "")),
        kind=str(payload.get("kind", "")),
        target=str(payload.get("target", "")),
        boundary=str(payload.get("boundary", "")),
        started_at=str(payload.get("started_at", "")),
        finished_at=str(payload.get("finished_at", "")),
        pre_identity=identity("pre_identity"),
        post_identity=identity("post_identity"),
        target_identity=str(payload.get("target_identity", "")),
        environment_identity_schema_version=identity_schema_version,
        environment_identity=str(payload.get("environment_identity", "")),
        requested_policy_hash=str(payload.get("requested_policy_hash", "")),
        effective_policy_hash=str(payload.get("effective_policy_hash", "")),
        status=StageReceiptStatus(str(payload.get("status", ""))),
        failure_class=(
            str(payload["failure_class"]) if payload.get("failure_class") is not None else None
        ),
        result_reference=(
            str(payload["result_reference"])
            if payload.get("result_reference") is not None
            else None
        ),
        artifact_digests=tuple(artifacts),
        cache_status=StageCacheStatus(str(payload.get("cache_status", ""))),
        source_changed=bool(payload.get("source_changed", False)),
        schema_version=schema_version,
    )
    return validate_stage_receipt(receipt)


def _effect_timestamp(value: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise RepoForgeError(
            f"Effect receipt {field} is not an ISO timestamp",
            code=ErrorCode.STATE_INVALID,
        ) from exc
    if parsed.tzinfo is None:
        _invalid(f"Effect receipt {field} must include a timezone offset")
    return parsed


def _effect_optional(value: str | None, field: str, *, limit: int = 2_000) -> str | None:
    if value is None:
        return None
    if (
        not value
        or len(value) > limit
        or any(ord(character) < 32 and character not in "\n\t" for character in value)
    ):
        _invalid(f"Effect receipt {field} is invalid")
    return value


def normalize_effect_identity(value: Mapping[str, Any] | None) -> EffectIdentity:
    if value is None:
        return ()
    if len(value) > 20:
        _invalid("Effect identity exceeds the 20-field limit")
    normalized: list[tuple[str, str]] = []
    for key, raw in sorted(value.items()):
        if _SAFE_ID.fullmatch(key) is None:
            _invalid("Effect identity key is invalid")
        if not isinstance(raw, (str, int, bool)):
            _invalid("Effect identity value must be a scalar")
        rendered = str(raw).lower() if isinstance(raw, bool) else str(raw)
        if (
            not rendered
            or len(rendered) > 512
            or any(ord(character) < 32 for character in rendered)
        ):
            _invalid("Effect identity value is invalid")
        normalized.append((key, rendered))
    return tuple(normalized)


def _validate_effect_identity(value: EffectIdentity, field: str) -> None:
    try:
        rendered = dict(value)
    except (TypeError, ValueError) as exc:
        raise RepoForgeError(
            f"Effect receipt {field} is invalid",
            code=ErrorCode.STATE_INVALID,
        ) from exc
    if len(rendered) != len(value) or value != normalize_effect_identity(rendered):
        _invalid(f"Effect receipt {field} is not canonical")


def validate_effect_receipt(receipt: EffectReceipt) -> EffectReceipt:
    if receipt.schema_version != EFFECT_RECEIPT_SCHEMA_VERSION:
        _invalid("Effect receipt schema version is unsupported")
    if _RECEIPT_ID.fullmatch(receipt.receipt_id) is None:
        _invalid("Effect receipt id is invalid")
    if _OPERATION_ID.fullmatch(receipt.operation_id) is None:
        _invalid("Effect receipt operation id is invalid")
    if _SAFE_ID.fullmatch(receipt.action) is None:
        _invalid("Effect receipt action is invalid")
    if (
        receipt.idempotency_key_hash is not None
        and _SHA64.fullmatch(receipt.idempotency_key_hash) is None
    ):
        _invalid("Effect receipt idempotency key hash is invalid")
    if _SHA64.fullmatch(receipt.request_fingerprint) is None:
        _invalid("Effect receipt request fingerprint is invalid")
    if _SAFE_ID.fullmatch(receipt.correlation_id) is None:
        _invalid("Effect receipt correlation id is invalid")
    if not isinstance(receipt.effect_boundary_crossed, bool):
        _invalid("Effect receipt boundary flag is invalid")
    _validate_effect_identity(receipt.pre_identity, "pre_identity")
    _validate_effect_identity(receipt.post_identity, "post_identity")
    accepted = _effect_timestamp(receipt.accepted_at, "accepted_at")
    updated = _effect_timestamp(receipt.updated_at, "updated_at")
    if updated < accepted:
        _invalid("Effect receipt updated_at precedes accepted_at")
    result_reference = _effect_optional(receipt.result_reference, "result_reference", limit=256)
    error_code = _effect_optional(receipt.error_code, "error_code", limit=128)
    error_message = _effect_optional(receipt.error_message, "error_message")
    if receipt.state in {
        EffectReceiptState.ACCEPTED,
        EffectReceiptState.APPLYING,
    } and any(value is not None for value in (result_reference, error_code, error_message)):
        _invalid("Non-terminal effect receipt cannot contain terminal evidence")
    if receipt.state in {
        EffectReceiptState.APPLIED_UNVALIDATED,
        EffectReceiptState.APPLIED_VALIDATED,
    } and (result_reference is None or error_code is not None):
        _invalid("Applied effect receipt requires a result reference and no error")
    if receipt.state is EffectReceiptState.FAILED_BEFORE_EFFECT and (
        result_reference is not None or error_code is None or receipt.effect_boundary_crossed
    ):
        _invalid("Failed-before-effect receipt requires error evidence without a result")
    if receipt.state is EffectReceiptState.FAILED_AFTER_EFFECT and (
        result_reference is None or error_code is None or not receipt.effect_boundary_crossed
    ):
        _invalid("Failed-after-effect receipt requires result and error evidence")
    if receipt.state is EffectReceiptState.ROLLED_BACK and (
        error_code is None or not receipt.effect_boundary_crossed
    ):
        _invalid("Rolled-back effect receipt requires boundary and compensation evidence")
    if receipt.state is EffectReceiptState.UNKNOWN and error_code is None:
        _invalid("Unknown effect receipt requires typed error evidence")
    return receipt


def create_effect_receipt(
    *,
    receipt_id: str,
    operation_id: str,
    action: str,
    idempotency_key_hash: str | None,
    request_fingerprint: str,
    accepted_at: str,
    correlation_id: str,
    pre_identity: Mapping[str, Any] | None = None,
) -> EffectReceipt:
    return validate_effect_receipt(
        EffectReceipt(
            receipt_id=receipt_id,
            operation_id=operation_id,
            action=action,
            idempotency_key_hash=idempotency_key_hash,
            request_fingerprint=request_fingerprint,
            state=EffectReceiptState.ACCEPTED,
            accepted_at=accepted_at,
            updated_at=accepted_at,
            correlation_id=correlation_id,
            pre_identity=normalize_effect_identity(pre_identity),
        )
    )


def transition_effect_receipt(
    receipt: EffectReceipt,
    new_state: EffectReceiptState,
    *,
    now: str,
    result_reference: str | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
    effect_boundary_crossed: bool | None = None,
    post_identity: Mapping[str, Any] | None = None,
) -> EffectReceipt:
    validate_effect_receipt(receipt)
    if new_state is receipt.state:
        return receipt
    if new_state not in _EFFECT_TRANSITIONS.get(receipt.state, frozenset()):
        _invalid(
            f"Effect receipt transition is not allowed: {receipt.state.value} -> {new_state.value}"
        )
    updated = replace(
        receipt,
        state=new_state,
        updated_at=now,
        result_reference=result_reference,
        error_code=error_code,
        error_message=error_message,
        effect_boundary_crossed=(
            receipt.effect_boundary_crossed
            if effect_boundary_crossed is None
            else effect_boundary_crossed
        ),
        post_identity=(
            receipt.post_identity
            if post_identity is None
            else normalize_effect_identity(post_identity)
        ),
    )
    return validate_effect_receipt(updated)


def effect_receipt_payload(receipt: EffectReceipt) -> dict[str, object]:
    validate_effect_receipt(receipt)
    return {
        "accepted_at": receipt.accepted_at,
        "action": receipt.action,
        "correlation_id": receipt.correlation_id,
        "error_code": receipt.error_code,
        "error_message": receipt.error_message,
        "effect_boundary_crossed": receipt.effect_boundary_crossed,
        "idempotency_key_hash": receipt.idempotency_key_hash,
        "operation_id": receipt.operation_id,
        "post_identity": dict(receipt.post_identity),
        "pre_identity": dict(receipt.pre_identity),
        "receipt_id": receipt.receipt_id,
        "request_fingerprint": receipt.request_fingerprint,
        "result_reference": receipt.result_reference,
        "schema_version": receipt.schema_version,
        "state": receipt.state.value,
        "updated_at": receipt.updated_at,
    }


def effect_receipt_from_payload(payload: dict[str, Any]) -> EffectReceipt:
    schema_version = payload.get("schema_version")
    if not isinstance(schema_version, int) or isinstance(schema_version, bool):
        _invalid("Effect receipt schema payload is invalid")
    try:
        state = EffectReceiptState(str(payload.get("state", "")))
    except ValueError as exc:
        raise RepoForgeError(
            "Effect receipt state payload is invalid",
            code=ErrorCode.STATE_INVALID,
        ) from exc
    boundary_crossed = payload.get("effect_boundary_crossed", False)
    if not isinstance(boundary_crossed, bool):
        _invalid("Effect receipt boundary payload is invalid")
    pre_identity = payload.get("pre_identity", {})
    post_identity = payload.get("post_identity", {})
    if not isinstance(pre_identity, dict) or not isinstance(post_identity, dict):
        _invalid("Effect receipt identity payload is invalid")
    return validate_effect_receipt(
        EffectReceipt(
            receipt_id=str(payload.get("receipt_id", "")),
            operation_id=str(payload.get("operation_id", "")),
            action=str(payload.get("action", "")),
            idempotency_key_hash=(
                str(payload["idempotency_key_hash"])
                if payload.get("idempotency_key_hash") is not None
                else None
            ),
            request_fingerprint=str(payload.get("request_fingerprint", "")),
            state=state,
            accepted_at=str(payload.get("accepted_at", "")),
            updated_at=str(payload.get("updated_at", "")),
            correlation_id=str(payload.get("correlation_id", "")),
            pre_identity=normalize_effect_identity(pre_identity),
            post_identity=normalize_effect_identity(post_identity),
            effect_boundary_crossed=boundary_crossed,
            result_reference=(
                str(payload["result_reference"])
                if payload.get("result_reference") is not None
                else None
            ),
            error_code=(
                str(payload["error_code"]) if payload.get("error_code") is not None else None
            ),
            error_message=(
                str(payload["error_message"]) if payload.get("error_message") is not None else None
            ),
            schema_version=schema_version,
        )
    )
