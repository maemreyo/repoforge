"""Immutable, bounded receipts for one accepted-plan stage execution."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, NoReturn

from .errors import ErrorCode, RepoForgeError

EXECUTION_RECEIPT_SCHEMA_VERSION = 2
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
