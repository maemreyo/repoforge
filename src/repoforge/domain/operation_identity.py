"""Durable operation-scoped repository identity lifecycle."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, replace
from datetime import datetime
from typing import TYPE_CHECKING

from .errors import ErrorCode, RepoForgeError
from .operation_task import validate_operation_id
from .repository_identity import (
    AuthLease,
    AuthLeaseState,
    AuthTargetKind,
    OperationIdentityContext,
)

if TYPE_CHECKING:
    from .operation_worker import OperationWorkerBinding
    from .task_capsule import TaskCapsule

_CONTEXT_ID = re.compile(r"^identity-[a-f0-9]{24}$")
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SHA256 = re.compile(r"^[a-f0-9]{64}$")


def _error(code: ErrorCode, message: str, *, retryable: bool = False) -> RepoForgeError:
    return RepoForgeError(
        message,
        code=code,
        retryable=retryable,
        unchanged_state=("No external repository write was admitted.",),
    )


def _timestamp(value: str, field_name: str) -> datetime:
    if not isinstance(value, str) or not value or len(value) > 80:
        raise ValueError(f"{field_name} must be a bounded timezone-aware timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field_name} must include a timezone offset")
    return parsed


def _safe_identifier(value: str, field_name: str) -> str:
    if not isinstance(value, str) or _SAFE_ID.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a bounded safe identifier")
    return value


@dataclass(frozen=True, slots=True)
class OperationIdentityReference:
    context_id: str
    context_digest: str

    def __post_init__(self) -> None:
        if not isinstance(self.context_id, str) or _CONTEXT_ID.fullmatch(self.context_id) is None:
            raise ValueError(
                "context_id must use identity- followed by 24 lowercase hex characters"
            )
        if (
            not isinstance(self.context_digest, str)
            or _SHA256.fullmatch(self.context_digest) is None
        ):
            raise ValueError("context_digest must be a lowercase SHA-256")

    def payload(self) -> dict[str, str]:
        return {"context_id": self.context_id, "context_digest": self.context_digest}


@dataclass(frozen=True, slots=True)
class LeaseCapabilityRequest:
    lease_id: str
    capability_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _safe_identifier(self.lease_id, "lease_id")
        if (
            not isinstance(self.capability_ids, tuple)
            or not self.capability_ids
            or len(self.capability_ids) > 64
        ):
            raise ValueError("capability_ids must be a non-empty bounded tuple")
        normalized = tuple(_safe_identifier(item, "capability_id") for item in self.capability_ids)
        if len(set(normalized)) != len(normalized):
            raise ValueError("capability_ids must be unique")


@dataclass(frozen=True, slots=True)
class OperationIdentityRecord:
    reference: OperationIdentityReference
    operation_id: str
    context: OperationIdentityContext
    capability_requests: tuple[LeaseCapabilityRequest, ...]
    superseded_lease_ids: tuple[str, ...]
    created_at: str
    updated_at: str

    def __post_init__(self) -> None:
        validate_operation_identity_record(self)

    def safe_payload(self) -> dict[str, object]:
        return {
            "reference": self.reference.payload(),
            "operation_id": self.operation_id,
            "context": self.context.payload(),
            "capability_requests": [
                {"lease_id": item.lease_id, "capability_ids": list(item.capability_ids)}
                for item in self.capability_requests
            ],
            "superseded_lease_ids": list(self.superseded_lease_ids),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


def _lease_identity_payload(lease: AuthLease) -> dict[str, object]:
    """Return only immutable identity fields, excluding renewable lifecycle material."""

    return {
        "profile_id": lease.profile_id,
        "provider": lease.provider.value,
        "repository_id": lease.repository_id,
        "target_kind": lease.target_kind.value,
        "target_id": lease.target_id,
        "actor_id": lease.actor_id,
        "credential_ref": lease.credential_ref.payload(),
        "config_revision": lease.config_revision,
        "policy_revision": lease.policy_revision,
        "provider_metadata": dict(lease.provider_metadata),
    }


def operation_identity_digest(context: OperationIdentityContext) -> str:
    if not isinstance(context, OperationIdentityContext):
        raise ValueError("context must be an OperationIdentityContext")
    identity_projection = {
        "operation_id": context.operation_id,
        "primary_repository_id": context.primary_repository_id,
        "actor_class": context.actor_class.value,
        "selected_at": context.selected_at,
        "config_revision": context.config_revision,
        "policy_revision": context.policy_revision,
        "auth_leases": sorted(
            (_lease_identity_payload(lease) for lease in context.auth_leases),
            key=lambda item: (str(item["target_kind"]), str(item["target_id"])),
        ),
    }
    encoded = json.dumps(
        identity_projection,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def validate_operation_identity_record(record: OperationIdentityRecord) -> OperationIdentityRecord:
    if not isinstance(record.reference, OperationIdentityReference):
        raise ValueError("reference must be an OperationIdentityReference")
    validate_operation_id(record.operation_id)
    if not isinstance(record.context, OperationIdentityContext):
        raise ValueError("context must be an OperationIdentityContext")
    if record.context.operation_id != record.operation_id:
        raise ValueError("operation identity context does not match its record operation")
    if record.reference.context_digest != operation_identity_digest(record.context):
        raise ValueError("operation identity context digest is stale or corrupt")
    if not isinstance(record.capability_requests, tuple) or not record.capability_requests:
        raise ValueError("capability_requests must be a non-empty tuple")
    if any(not isinstance(item, LeaseCapabilityRequest) for item in record.capability_requests):
        raise ValueError("capability_requests must contain LeaseCapabilityRequest values")
    request_ids = tuple(item.lease_id for item in record.capability_requests)
    lease_ids = tuple(item.lease_id for item in record.context.auth_leases)
    if len(set(request_ids)) != len(request_ids) or set(request_ids) != set(lease_ids):
        raise ValueError("capability requests must cover each current auth lease exactly once")
    if not isinstance(record.superseded_lease_ids, tuple) or len(record.superseded_lease_ids) > 256:
        raise ValueError("superseded_lease_ids must be a bounded tuple")
    superseded = tuple(
        _safe_identifier(item, "superseded lease ID") for item in record.superseded_lease_ids
    )
    if len(set(superseded)) != len(superseded) or set(superseded).intersection(lease_ids):
        raise ValueError("superseded lease IDs must be unique and not current")
    created = _timestamp(record.created_at, "created_at")
    updated = _timestamp(record.updated_at, "updated_at")
    if updated < created:
        raise ValueError("updated_at cannot precede created_at")
    return record


def new_operation_identity_record(
    context: OperationIdentityContext,
    *,
    context_id: str,
    capability_requests: tuple[LeaseCapabilityRequest, ...],
    now: str,
) -> OperationIdentityRecord:
    reference = OperationIdentityReference(context_id, operation_identity_digest(context))
    return OperationIdentityRecord(
        reference=reference,
        operation_id=validate_operation_id(context.operation_id),
        context=context,
        capability_requests=capability_requests,
        superseded_lease_ids=(),
        created_at=now,
        updated_at=now,
    )


def _lease_request(record: OperationIdentityRecord, lease_id: str) -> LeaseCapabilityRequest:
    for request in record.capability_requests:
        if request.lease_id == lease_id:
            return request
    raise _error(
        ErrorCode.OPERATION_IDENTITY_MISMATCH,
        "Operation identity capability request is missing for the selected lease.",
    )


def require_operation_lease(
    record: OperationIdentityRecord,
    *,
    operation_id: str,
    target_kind: AuthTargetKind,
    target_id: str,
    capability_id: str,
    now: str,
) -> AuthLease:
    validate_operation_identity_record(record)
    if operation_id != record.operation_id:
        raise _error(
            ErrorCode.OPERATION_IDENTITY_MISMATCH,
            "Operation identity record cannot be consumed by another operation.",
        )
    _safe_identifier(capability_id, "capability_id")
    selected = next(
        (
            lease
            for lease in record.context.auth_leases
            if lease.target_kind is target_kind and lease.target_id == target_id
        ),
        None,
    )
    if selected is None:
        raise _error(
            ErrorCode.OPERATION_IDENTITY_MISMATCH,
            "No operation-scoped auth lease exists for the exact requested target.",
        )
    request = _lease_request(record, selected.lease_id)
    if capability_id not in request.capability_ids:
        raise _error(
            ErrorCode.CREDENTIAL_CAPABILITY_DENIED,
            "The operation-scoped auth lease does not include the requested capability.",
        )
    if selected.state is AuthLeaseState.REVOKED:
        raise _error(ErrorCode.CREDENTIAL_REVOKED, "The operation-scoped auth lease was revoked.")
    if selected.state in {AuthLeaseState.EXPIRED, AuthLeaseState.RELEASED}:
        raise _error(ErrorCode.CREDENTIAL_EXPIRED, "The operation-scoped auth lease is not active.")
    if _timestamp(selected.expires_at, "expires_at") <= _timestamp(now, "now"):
        raise _error(ErrorCode.CREDENTIAL_EXPIRED, "The operation-scoped auth lease expired.")
    return selected


def _refresh_identity(lease: AuthLease) -> tuple[object, ...]:
    return (
        lease.profile_id,
        lease.provider,
        lease.repository_id,
        lease.target_kind,
        lease.target_id,
        lease.actor_id,
        lease.credential_ref,
        lease.config_revision,
        lease.policy_revision,
        lease.provider_metadata,
    )


def refresh_operation_lease(
    record: OperationIdentityRecord,
    replacement: AuthLease,
    *,
    now: str,
) -> OperationIdentityRecord:
    validate_operation_identity_record(record)
    current_index = next(
        (
            index
            for index, lease in enumerate(record.context.auth_leases)
            if lease.target_kind is replacement.target_kind
            and lease.target_id == replacement.target_id
        ),
        None,
    )
    if current_index is None:
        raise _error(
            ErrorCode.CREDENTIAL_REFRESH_IDENTITY_MISMATCH,
            "Refreshed auth material targets an unbound operation resource.",
        )
    current = record.context.auth_leases[current_index]
    if (
        replacement.lease_id == current.lease_id
        or replacement.state is not AuthLeaseState.ACTIVE
        or _refresh_identity(replacement) != _refresh_identity(current)
    ):
        raise _error(
            ErrorCode.CREDENTIAL_REFRESH_IDENTITY_MISMATCH,
            "Refreshed auth material changed a locked operation identity field.",
        )
    now_dt = _timestamp(now, "now")
    if (
        _timestamp(replacement.issued_at, "issued_at") > now_dt
        or _timestamp(replacement.expires_at, "expires_at") <= now_dt
    ):
        raise _error(
            ErrorCode.CREDENTIAL_REFRESH_IDENTITY_MISMATCH,
            "Refreshed auth material has an invalid lifetime for this operation.",
        )
    leases = list(record.context.auth_leases)
    leases[current_index] = replacement
    requests = tuple(
        LeaseCapabilityRequest(replacement.lease_id, item.capability_ids)
        if item.lease_id == current.lease_id
        else item
        for item in record.capability_requests
    )
    context = replace(record.context, auth_leases=tuple(leases))
    return OperationIdentityRecord(
        reference=OperationIdentityReference(
            record.reference.context_id,
            operation_identity_digest(context),
        ),
        operation_id=record.operation_id,
        context=context,
        capability_requests=requests,
        superseded_lease_ids=(*record.superseded_lease_ids, current.lease_id),
        created_at=record.created_at,
        updated_at=now,
    )


def revoke_operation_leases(
    record: OperationIdentityRecord,
    *,
    now: str,
    lease_id: str | None = None,
    profile_id: str | None = None,
) -> OperationIdentityRecord:
    validate_operation_identity_record(record)
    if (lease_id is None) == (profile_id is None):
        raise ValueError("exactly one of lease_id or profile_id is required")
    if lease_id is not None:
        _safe_identifier(lease_id, "lease_id")
    if profile_id is not None:
        _safe_identifier(profile_id, "profile_id")
    matched = False
    leases: list[AuthLease] = []
    for lease in record.context.auth_leases:
        selected = (
            lease.lease_id == lease_id if lease_id is not None else lease.profile_id == profile_id
        )
        if selected:
            matched = True
            leases.append(replace(lease, state=AuthLeaseState.REVOKED))
        else:
            leases.append(lease)
    if not matched:
        raise _error(ErrorCode.OPERATION_IDENTITY_NOT_FOUND, "No matching auth lease was found.")
    context = replace(record.context, auth_leases=tuple(leases))
    return replace(
        record,
        reference=OperationIdentityReference(
            record.reference.context_id,
            operation_identity_digest(context),
        ),
        context=context,
        updated_at=now,
    )


def expire_operation_leases(
    record: OperationIdentityRecord,
    *,
    now: str,
) -> OperationIdentityRecord:
    validate_operation_identity_record(record)
    now_dt = _timestamp(now, "now")
    changed = False
    leases: list[AuthLease] = []
    for lease in record.context.auth_leases:
        if (
            lease.state is AuthLeaseState.ACTIVE
            and _timestamp(lease.expires_at, "expires_at") <= now_dt
        ):
            changed = True
            leases.append(replace(lease, state=AuthLeaseState.EXPIRED))
        else:
            leases.append(lease)
    if not changed:
        return record
    context = replace(record.context, auth_leases=tuple(leases))
    return replace(
        record,
        reference=OperationIdentityReference(
            record.reference.context_id,
            operation_identity_digest(context),
        ),
        context=context,
        updated_at=now,
    )


def bind_worker_identity(
    binding: OperationWorkerBinding,
    reference: OperationIdentityReference,
) -> OperationWorkerBinding:
    if binding.operation_id == "":
        raise ValueError("worker binding operation_id is required")
    return replace(
        binding,
        identity_context_id=reference.context_id,
        identity_context_digest=reference.context_digest,
    )


def bind_task_identity(
    task: TaskCapsule,
    reference: OperationIdentityReference,
    *,
    updated_at: str,
) -> TaskCapsule:
    existing = next(
        (item for item in task.identity_contexts if item.context_id == reference.context_id),
        None,
    )
    if existing is not None:
        if existing != reference:
            raise _error(
                ErrorCode.OPERATION_IDENTITY_MISMATCH,
                "Task capsule already binds this identity context ID to another digest.",
            )
        return task
    return replace(
        task,
        identity_contexts=(*task.identity_contexts, reference),
        updated_at=updated_at,
    )
