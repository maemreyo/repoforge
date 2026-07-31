"""Pure exact-intent review for package and LFS publication effects."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime

from .errors import ErrorCode, RepoForgeError
from .nested_identity import (
    NestedAccess,
    NestedBindingState,
    NestedResourceKind,
    NestedResourceTarget,
)
from .operation_identity import LeaseCapabilityRequest
from .repository_identity import AuthLease, AuthLeaseState, AuthTargetKind

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_SUPPORTED_KINDS = frozenset({NestedResourceKind.LFS, NestedResourceKind.PACKAGE})


def _safe_id(value: str, field_name: str) -> str:
    if not isinstance(value, str) or _SAFE_ID.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a bounded safe identifier")
    return value


def _optional_safe_id(value: str | None, field_name: str) -> str | None:
    return None if value is None else _safe_id(value, field_name)


def _sha256(value: str, field_name: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a full lowercase SHA-256")
    return value


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


def _digest(payload: object) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _error(code: ErrorCode, message: str, *, target_id: str | None = None) -> RepoForgeError:
    details: dict[str, object] = {}
    if target_id is not None:
        details["target_id"] = target_id
    return RepoForgeError(
        message,
        code=code,
        retryable=False,
        unchanged_state=("No nested publication effect was started.",),
        safe_next_action="Re-review the exact nested target, lease, and payload before retrying.",
        details=details,
    )


@dataclass(frozen=True, slots=True)
class NestedPublicationIntent:
    publication_id: str
    operation_id: str
    resource_kind: NestedResourceKind
    source_repository_id: str
    destination_target_id: str
    endpoint_digest: str
    payload_digest: str
    capability_digest: str
    permission_digest: str
    cross_boundary_approval_id: str | None = None

    def __post_init__(self) -> None:
        _safe_id(self.publication_id, "publication_id")
        _safe_id(self.operation_id, "operation_id")
        if self.resource_kind not in _SUPPORTED_KINDS:
            raise ValueError("nested publication resource_kind must be LFS or PACKAGE")
        _safe_id(self.source_repository_id, "source_repository_id")
        _safe_id(self.destination_target_id, "destination_target_id")
        _sha256(self.endpoint_digest, "endpoint_digest")
        _sha256(self.payload_digest, "payload_digest")
        _sha256(self.capability_digest, "capability_digest")
        _sha256(self.permission_digest, "permission_digest")
        _optional_safe_id(self.cross_boundary_approval_id, "cross_boundary_approval_id")

    def payload(self) -> dict[str, object]:
        return {
            "publication_id": self.publication_id,
            "operation_id": self.operation_id,
            "resource_kind": self.resource_kind.value,
            "source_repository_id": self.source_repository_id,
            "destination_target_id": self.destination_target_id,
            "endpoint_digest": self.endpoint_digest,
            "payload_digest": self.payload_digest,
            "capability_digest": self.capability_digest,
            "permission_digest": self.permission_digest,
            "cross_boundary_approval_id": self.cross_boundary_approval_id,
        }


@dataclass(frozen=True, slots=True)
class ReviewedNestedPublication:
    intent: NestedPublicationIntent
    lease_id: str
    profile_id: str
    repository_id: str
    target_kind: AuthTargetKind
    target_id: str
    config_revision: str
    policy_revision: str
    review_digest: str

    def __post_init__(self) -> None:
        if not isinstance(self.intent, NestedPublicationIntent):
            raise ValueError("intent must be a NestedPublicationIntent")
        _safe_id(self.lease_id, "lease_id")
        _safe_id(self.profile_id, "profile_id")
        _safe_id(self.repository_id, "repository_id")
        if not isinstance(self.target_kind, AuthTargetKind):
            raise ValueError("target_kind must be an AuthTargetKind")
        _safe_id(self.target_id, "target_id")
        _sha256(self.config_revision, "config_revision")
        _sha256(self.policy_revision, "policy_revision")
        _sha256(self.review_digest, "review_digest")

    @property
    def endpoint_digest(self) -> str:
        return self.intent.endpoint_digest

    @property
    def payload_digest(self) -> str:
        return self.intent.payload_digest

    def safe_payload(self) -> dict[str, object]:
        return {
            "publication_id": self.intent.publication_id,
            "operation_id": self.intent.operation_id,
            "intent": self.intent.payload(),
            "lease_id": self.lease_id,
            "profile_id": self.profile_id,
            "repository_id": self.repository_id,
            "target_kind": self.target_kind.value,
            "target_id": self.target_id,
            "config_revision": self.config_revision,
            "policy_revision": self.policy_revision,
            "review_digest": self.review_digest,
        }


def _require_target(intent: NestedPublicationIntent, target: NestedResourceTarget) -> None:
    if not isinstance(target, NestedResourceTarget):
        raise TypeError("target must be a NestedResourceTarget")
    if (
        target.kind is not intent.resource_kind
        or target.access is not NestedAccess.WRITE
        or target.binding_state is not NestedBindingState.EXACT
        or target.profile_id is None
        or target.target_id != intent.destination_target_id
        or target.endpoint_digest != intent.endpoint_digest
    ):
        raise _error(
            ErrorCode.PUBLICATION_TARGET_MISMATCH,
            "Nested publication target differs from the exact reviewed intent.",
            target_id=intent.destination_target_id,
        )
    if target.owner_boundary != target.primary_owner_boundary and (
        intent.cross_boundary_approval_id is None
    ):
        raise _error(
            ErrorCode.CROSS_BOUNDARY_PUBLICATION_DENIED,
            "Cross-boundary nested publication requires an exact approval identity.",
            target_id=target.target_id,
        )


def _require_lease(
    target: NestedResourceTarget,
    lease: AuthLease,
    *,
    now: str,
) -> None:
    if not isinstance(lease, AuthLease):
        raise TypeError("lease must be an AuthLease")
    observed_at = _timestamp(now, "now")
    if (
        lease.state is AuthLeaseState.EXPIRED
        or _timestamp(lease.expires_at, "lease.expires_at") <= observed_at
    ):
        raise _error(
            ErrorCode.CREDENTIAL_EXPIRED,
            "Nested publication lease expired before the write boundary.",
            target_id=target.target_id,
        )
    if lease.state is not AuthLeaseState.ACTIVE:
        raise _error(
            ErrorCode.CREDENTIAL_REVOKED,
            "Nested publication lease is not active.",
            target_id=target.target_id,
        )
    if _timestamp(lease.issued_at, "lease.issued_at") > observed_at:
        raise _error(
            ErrorCode.OPERATION_IDENTITY_MISMATCH,
            "Nested publication lease was issued after the review boundary.",
            target_id=target.target_id,
        )
    if (
        lease.target_kind is not target.target_kind
        or lease.target_id != target.target_id
        or lease.provider is not target.provider
        or lease.profile_id != target.profile_id
        or (target.repository_id is not None and lease.repository_id != target.repository_id)
    ):
        raise _error(
            ErrorCode.OPERATION_IDENTITY_MISMATCH,
            "Nested publication lease does not match the exact target identity.",
            target_id=target.target_id,
        )
    if dict(lease.provider_metadata).get("nested_endpoint_digest") != target.endpoint_digest:
        raise _error(
            ErrorCode.PUBLICATION_TARGET_MISMATCH,
            "Nested publication lease endpoint differs from the reviewed target.",
            target_id=target.target_id,
        )


def _is_write_capability(value: str) -> bool:
    return value.endswith((".write", "_write"))


def _require_capabilities(
    target: NestedResourceTarget,
    lease: AuthLease,
    capability_request: LeaseCapabilityRequest,
) -> None:
    if not isinstance(capability_request, LeaseCapabilityRequest):
        raise TypeError("capability_request must be a LeaseCapabilityRequest")
    if (
        capability_request.lease_id != lease.lease_id
        or capability_request.capability_ids != target.capability_ids
        or not any(_is_write_capability(item) for item in capability_request.capability_ids)
    ):
        raise _error(
            ErrorCode.CREDENTIAL_CAPABILITY_DENIED,
            "Nested publication requires the exact target-bound write capability request.",
            target_id=target.target_id,
        )


def review_nested_publication(
    intent: NestedPublicationIntent,
    *,
    target: NestedResourceTarget,
    lease: AuthLease,
    capability_request: LeaseCapabilityRequest,
    observed_capability_digest: str,
    observed_permission_digest: str,
    now: str,
) -> ReviewedNestedPublication:
    """Review one package or LFS write immediately before its effect boundary."""

    if not isinstance(intent, NestedPublicationIntent):
        raise TypeError("intent must be a NestedPublicationIntent")
    _sha256(observed_capability_digest, "observed_capability_digest")
    _sha256(observed_permission_digest, "observed_permission_digest")
    _require_target(intent, target)
    _require_lease(target, lease, now=now)
    _require_capabilities(target, lease, capability_request)
    if observed_capability_digest != intent.capability_digest:
        raise _error(
            ErrorCode.CREDENTIAL_CAPABILITY_DENIED,
            "Nested publication capability evidence changed before effect.",
            target_id=target.target_id,
        )
    if observed_permission_digest != intent.permission_digest:
        raise _error(
            ErrorCode.GITHUB_API_PERMISSION_DENIED,
            "Nested publication permission evidence changed before effect.",
            target_id=target.target_id,
        )

    review_payload = {
        "intent": intent.payload(),
        "target": {
            "provider": target.provider.value,
            "repository_id": target.repository_id,
            "target_kind": target.target_kind.value,
            "target_id": target.target_id,
            "profile_id": target.profile_id,
            "owner_boundary": target.owner_boundary,
            "primary_owner_boundary": target.primary_owner_boundary,
            "endpoint_digest": target.endpoint_digest,
        },
        "lease_id": lease.lease_id,
        "lease_repository_id": lease.repository_id,
        "config_revision": lease.config_revision,
        "policy_revision": lease.policy_revision,
        "capability_ids": list(capability_request.capability_ids),
        "observed_capability_digest": observed_capability_digest,
        "observed_permission_digest": observed_permission_digest,
    }
    return ReviewedNestedPublication(
        intent=intent,
        lease_id=lease.lease_id,
        profile_id=lease.profile_id,
        repository_id=lease.repository_id,
        target_kind=lease.target_kind,
        target_id=lease.target_id,
        config_revision=lease.config_revision,
        policy_revision=lease.policy_revision,
        review_digest=_digest(review_payload),
    )


def revalidate_nested_publication(
    reviewed: ReviewedNestedPublication,
    *,
    intent: NestedPublicationIntent,
    target: NestedResourceTarget,
    lease: AuthLease,
    capability_request: LeaseCapabilityRequest,
    observed_capability_digest: str,
    observed_permission_digest: str,
    now: str,
) -> ReviewedNestedPublication:
    """Fail closed when any exact nested publication evidence changed after review."""

    if not isinstance(reviewed, ReviewedNestedPublication):
        raise TypeError("reviewed must be a ReviewedNestedPublication")
    current = review_nested_publication(
        intent,
        target=target,
        lease=lease,
        capability_request=capability_request,
        observed_capability_digest=observed_capability_digest,
        observed_permission_digest=observed_permission_digest,
        now=now,
    )
    if current != reviewed:
        raise _error(
            ErrorCode.PUBLICATION_TARGET_MISMATCH,
            "Nested publication identity or payload changed after review.",
            target_id=reviewed.target_id,
        )
    return reviewed


__all__ = [
    "NestedPublicationIntent",
    "ReviewedNestedPublication",
    "revalidate_nested_publication",
    "review_nested_publication",
]
