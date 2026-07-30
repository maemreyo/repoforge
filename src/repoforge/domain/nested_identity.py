"""Pure, secret-free routing contracts for nested repository resources."""

from __future__ import annotations

import hashlib
import posixpath
import re
from dataclasses import dataclass
from enum import Enum
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

from .repository_identity import (
    AuthTargetKind,
    RecoveryAction,
    RecoveryActionKind,
    RepositoryAuthFailureCode,
    RepositoryProvider,
)

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_PROVIDER_HOST = re.compile(r"^[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?$")
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_SCP_ENDPOINT = re.compile(
    r"^git@(?P<host>[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])):(?P<path>[^\s]+)$",
    re.IGNORECASE,
)
_MAX_ENDPOINT = 2_048
_MAX_LOCATION = 1_024
_MAX_DEPTH = 32
_MAX_CAPABILITIES = 32


class NestedResourceKind(str, Enum):
    SUBMODULE = "submodule"
    LFS = "lfs"
    PACKAGE = "package"
    RELEASE = "release"


class NestedAccess(str, Enum):
    READ = "read"
    WRITE = "write"


class NestedBindingState(str, Enum):
    EXACT = "exact"
    MISSING = "missing"
    AMBIGUOUS = "ambiguous"
    STALE = "stale"
    DISABLED = "disabled"
    TRANSFERRED = "transferred"


class NestedRoutingStatus(str, Enum):
    BOUND_PROFILE = "bound_profile"
    ANONYMOUS_READ = "anonymous_read"
    DENIED = "denied"


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


def _bounded_text(value: str, field_name: str, *, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or any(ord(character) < 32 for character in value)
    ):
        raise ValueError(f"{field_name} must be bounded non-empty text")
    return value


def _canonical_url(value: str) -> str:
    parsed = urlsplit(value)
    scheme = parsed.scheme.lower()
    if scheme not in {"https", "ssh"}:
        raise ValueError("nested endpoint must use reviewed HTTPS or SSH transport")
    if parsed.query or parsed.fragment:
        raise ValueError("nested endpoint cannot contain query or fragment data")
    if parsed.password is not None:
        raise ValueError("nested endpoint cannot contain credential userinfo")
    if scheme == "https" and parsed.username is not None:
        raise ValueError("HTTPS nested endpoint cannot contain userinfo")
    if scheme == "ssh" and parsed.username not in {None, "git"}:
        raise ValueError("SSH nested endpoint may use only the non-secret git transport principal")
    host = parsed.hostname
    if host is None or _PROVIDER_HOST.fullmatch(host.lower()) is None:
        raise ValueError("nested endpoint must contain a bounded provider host")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("nested endpoint port is invalid") from exc
    if port is not None and (scheme, port) not in {("https", 443), ("ssh", 22)}:
        raise ValueError("nested endpoint may use only the default reviewed transport port")
    decoded_path = unquote(parsed.path)
    if (
        not decoded_path.startswith("/")
        or decoded_path == "/"
        or any(ord(character) < 32 or character.isspace() for character in decoded_path)
    ):
        raise ValueError("nested endpoint path is invalid")
    path_parts = decoded_path.split("/")
    if any(part in {".", ".."} for part in path_parts):
        raise ValueError("nested endpoint path cannot traverse")
    normalized_path = posixpath.normpath(decoded_path).rstrip("/")
    if normalized_path in {"", ".", "/"}:
        raise ValueError("nested endpoint path is invalid")
    user = "git@" if scheme == "ssh" and parsed.username == "git" else ""
    port_text = f":{port}" if port is not None else ""
    authority = f"{user}{host.lower()}{port_text}"
    return urlunsplit((scheme, authority, normalized_path, "", ""))


def canonical_nested_endpoint(value: str, *, base_endpoint: str | None = None) -> str:
    """Return one credential-free canonical endpoint for routing and digesting."""

    _bounded_text(value, "nested endpoint", maximum=_MAX_ENDPOINT)
    if any(character.isspace() for character in value):
        raise ValueError("nested endpoint cannot contain whitespace")
    scp_match = _SCP_ENDPOINT.fullmatch(value)
    if scp_match is not None:
        value = f"ssh://git@{scp_match.group('host')}/{scp_match.group('path')}"
    elif urlsplit(value).scheme == "":
        if base_endpoint is None:
            raise ValueError("relative nested endpoint requires a reviewed base endpoint")
        if value.startswith(("/", "\\")) or "\\" in value:
            raise ValueError("relative nested endpoint cannot be local or absolute")
        base = _canonical_url(base_endpoint)
        value = urljoin(f"{base.rstrip('/')}/", value)
    return _canonical_url(value)


def nested_endpoint_digest(canonical_endpoint: str) -> str:
    canonical = canonical_nested_endpoint(canonical_endpoint)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _capability_ids(values: tuple[str, ...], *, allow_empty: bool = False) -> tuple[str, ...]:
    if (
        not isinstance(values, tuple)
        or (not values and not allow_empty)
        or len(values) > _MAX_CAPABILITIES
    ):
        raise ValueError("capability_ids must be a bounded tuple")
    normalized = tuple(_safe_id(item, "capability_id") for item in values)
    if len(set(normalized)) != len(normalized):
        raise ValueError("capability_ids must be unique")
    return normalized


def _source_locations(values: tuple[str, ...]) -> tuple[str, ...]:
    if not isinstance(values, tuple) or not values or len(values) > 128:
        raise ValueError("source_locations must be a non-empty bounded tuple")
    normalized: list[str] = []
    for value in values:
        _bounded_text(value, "source_location", maximum=_MAX_LOCATION)
        if value.startswith(("/", "../")) or "\x00" in value:
            raise ValueError("source_location must remain inside the reviewed repository")
        normalized.append(value)
    if len(set(normalized)) != len(normalized):
        raise ValueError("source_locations must be unique")
    return tuple(normalized)


def _expected_target_kind(kind: NestedResourceKind) -> AuthTargetKind:
    return {
        NestedResourceKind.SUBMODULE: AuthTargetKind.SUBMODULE,
        NestedResourceKind.LFS: AuthTargetKind.LFS,
        NestedResourceKind.PACKAGE: AuthTargetKind.PACKAGE,
        NestedResourceKind.RELEASE: AuthTargetKind.RELEASE,
    }[kind]


@dataclass(frozen=True, slots=True)
class NestedResourceCandidate:
    kind: NestedResourceKind
    access: NestedAccess
    canonical_endpoint: str
    source_location: str
    depth: int
    endpoint_digest: str

    def __post_init__(self) -> None:
        if not isinstance(self.kind, NestedResourceKind):
            raise ValueError("kind must be a NestedResourceKind")
        if not isinstance(self.access, NestedAccess):
            raise ValueError("access must be a NestedAccess")
        canonical = canonical_nested_endpoint(self.canonical_endpoint)
        if canonical != self.canonical_endpoint:
            raise ValueError("canonical_endpoint must already be canonical")
        _source_locations((self.source_location,))
        if (
            not isinstance(self.depth, int)
            or isinstance(self.depth, bool)
            or not 0 <= self.depth <= _MAX_DEPTH
        ):
            raise ValueError(f"depth must be between 0 and {_MAX_DEPTH}")
        _sha256(self.endpoint_digest, "endpoint_digest")
        if self.endpoint_digest != nested_endpoint_digest(canonical):
            raise ValueError("endpoint_digest does not match canonical_endpoint")

    def payload(self) -> dict[str, object]:
        return {
            "kind": self.kind.value,
            "access": self.access.value,
            "canonical_endpoint": self.canonical_endpoint,
            "source_location": self.source_location,
            "depth": self.depth,
            "endpoint_digest": self.endpoint_digest,
        }


@dataclass(frozen=True, slots=True)
class NestedResourceTarget:
    kind: NestedResourceKind
    access: NestedAccess
    provider: RepositoryProvider
    provider_host: str
    target_kind: AuthTargetKind
    target_id: str
    repository_id: str | None
    owner_boundary: str
    primary_owner_boundary: str
    capability_ids: tuple[str, ...]
    endpoint_digest: str
    binding_state: NestedBindingState
    profile_id: str | None
    public_read: bool

    def __post_init__(self) -> None:
        if not isinstance(self.kind, NestedResourceKind):
            raise ValueError("kind must be a NestedResourceKind")
        if not isinstance(self.access, NestedAccess):
            raise ValueError("access must be a NestedAccess")
        if not isinstance(self.provider, RepositoryProvider):
            raise ValueError("provider must be a RepositoryProvider")
        if (
            not isinstance(self.provider_host, str)
            or _PROVIDER_HOST.fullmatch(self.provider_host) is None
        ):
            raise ValueError("provider_host must be a bounded lowercase host")
        if not isinstance(self.target_kind, AuthTargetKind):
            raise ValueError("target_kind must be an AuthTargetKind")
        if self.target_kind is not _expected_target_kind(self.kind):
            raise ValueError("target_kind must match the nested resource kind")
        _safe_id(self.target_id, "target_id")
        _optional_safe_id(self.repository_id, "repository_id")
        _safe_id(self.owner_boundary, "owner_boundary")
        _safe_id(self.primary_owner_boundary, "primary_owner_boundary")
        _capability_ids(self.capability_ids)
        _sha256(self.endpoint_digest, "endpoint_digest")
        if not isinstance(self.binding_state, NestedBindingState):
            raise ValueError("binding_state must be a NestedBindingState")
        _optional_safe_id(self.profile_id, "profile_id")
        if self.binding_state is NestedBindingState.EXACT and self.profile_id is None:
            raise ValueError("exact nested binding requires an explicit profile")
        if self.binding_state is not NestedBindingState.EXACT and self.profile_id is not None:
            raise ValueError("non-exact nested binding cannot select a profile")
        if not isinstance(self.public_read, bool):
            raise ValueError("public_read must be boolean")
        if self.public_read and self.access is not NestedAccess.READ:
            raise ValueError("public access can only be represented for reads")

    def payload(self) -> dict[str, object]:
        return {
            "kind": self.kind.value,
            "access": self.access.value,
            "provider": self.provider.value,
            "provider_host": self.provider_host,
            "target_kind": self.target_kind.value,
            "target_id": self.target_id,
            "repository_id": self.repository_id,
            "owner_boundary": self.owner_boundary,
            "primary_owner_boundary": self.primary_owner_boundary,
            "capability_ids": list(self.capability_ids),
            "endpoint_digest": self.endpoint_digest,
            "binding_state": self.binding_state.value,
            "profile_id": self.profile_id,
            "public_read": self.public_read,
        }


@dataclass(frozen=True, slots=True)
class NestedRoutingDecision:
    status: NestedRoutingStatus
    target_kind: AuthTargetKind
    target_id: str
    profile_id: str | None
    capability_ids: tuple[str, ...]
    endpoint_digest: str
    failure_code: RepositoryAuthFailureCode | None
    recovery_actions: tuple[RecoveryAction, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.status, NestedRoutingStatus):
            raise ValueError("status must be a NestedRoutingStatus")
        if not isinstance(self.target_kind, AuthTargetKind):
            raise ValueError("target_kind must be an AuthTargetKind")
        _safe_id(self.target_id, "target_id")
        _optional_safe_id(self.profile_id, "profile_id")
        _capability_ids(self.capability_ids, allow_empty=True)
        _sha256(self.endpoint_digest, "endpoint_digest")
        if self.failure_code is not None and not isinstance(
            self.failure_code, RepositoryAuthFailureCode
        ):
            raise ValueError("failure_code must be a RepositoryAuthFailureCode")
        if not isinstance(self.recovery_actions, tuple) or len(self.recovery_actions) > 8:
            raise ValueError("recovery_actions must be a bounded tuple")
        if any(not isinstance(action, RecoveryAction) for action in self.recovery_actions):
            raise ValueError("recovery_actions must contain RecoveryAction values")
        if self.status is NestedRoutingStatus.BOUND_PROFILE:
            if self.profile_id is None or not self.capability_ids or self.failure_code is not None:
                raise ValueError("bound routing requires profile and capabilities without failure")
        elif self.status is NestedRoutingStatus.ANONYMOUS_READ:
            if self.profile_id is not None or self.capability_ids or self.failure_code is not None:
                raise ValueError("anonymous routing cannot carry profile, capabilities, or failure")
        elif self.failure_code is None:
            raise ValueError("denied routing requires a typed failure code")

    def payload(self) -> dict[str, object]:
        return {
            "status": self.status.value,
            "target_kind": self.target_kind.value,
            "target_id": self.target_id,
            "profile_id": self.profile_id,
            "capability_ids": list(self.capability_ids),
            "endpoint_digest": self.endpoint_digest,
            "failure_code": self.failure_code.value if self.failure_code is not None else None,
            "recovery_actions": [action.payload() for action in self.recovery_actions],
        }


@dataclass(frozen=True, slots=True)
class NestedIdentityReceipt:
    target_kind: AuthTargetKind
    target_id: str
    repository_id: str | None
    endpoint_digest: str
    routing_status: NestedRoutingStatus
    profile_id: str | None
    lease_id: str | None
    capability_ids: tuple[str, ...]
    source_locations: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.target_kind, AuthTargetKind):
            raise ValueError("target_kind must be an AuthTargetKind")
        _safe_id(self.target_id, "target_id")
        _optional_safe_id(self.repository_id, "repository_id")
        _sha256(self.endpoint_digest, "endpoint_digest")
        if not isinstance(self.routing_status, NestedRoutingStatus):
            raise ValueError("routing_status must be a NestedRoutingStatus")
        _optional_safe_id(self.profile_id, "profile_id")
        _optional_safe_id(self.lease_id, "lease_id")
        _capability_ids(self.capability_ids, allow_empty=True)
        _source_locations(self.source_locations)
        if self.routing_status is NestedRoutingStatus.BOUND_PROFILE:
            if self.profile_id is None or self.lease_id is None or not self.capability_ids:
                raise ValueError("bound receipt requires profile, lease, and capabilities")
        elif self.routing_status is NestedRoutingStatus.ANONYMOUS_READ:
            if self.profile_id is not None or self.lease_id is not None or self.capability_ids:
                raise ValueError("anonymous receipt cannot carry credentialed identity metadata")
        else:
            if self.lease_id is not None:
                raise ValueError("denied receipt cannot carry a lease")

    def payload(self) -> dict[str, object]:
        return {
            "target_kind": self.target_kind.value,
            "target_id": self.target_id,
            "repository_id": self.repository_id,
            "endpoint_digest": self.endpoint_digest,
            "routing_status": self.routing_status.value,
            "profile_id": self.profile_id,
            "lease_id": self.lease_id,
            "capability_ids": list(self.capability_ids),
            "source_locations": list(self.source_locations),
        }


def _denied(
    target: NestedResourceTarget,
    code: RepositoryAuthFailureCode,
    actions: tuple[RecoveryAction, ...],
) -> NestedRoutingDecision:
    return NestedRoutingDecision(
        status=NestedRoutingStatus.DENIED,
        target_kind=target.target_kind,
        target_id=target.target_id,
        profile_id=None,
        capability_ids=(),
        endpoint_digest=target.endpoint_digest,
        failure_code=code,
        recovery_actions=actions,
    )


def route_nested_resource(
    target: NestedResourceTarget,
    *,
    allow_anonymous_public_read: bool,
    exact_cross_boundary_approval_id: str | None,
    publication_intent_id: str | None,
) -> NestedRoutingDecision:
    """Apply fail-closed nested routing without consulting a primary credential."""

    if not isinstance(target, NestedResourceTarget):
        raise ValueError("target must be a NestedResourceTarget")
    if not isinstance(allow_anonymous_public_read, bool):
        raise ValueError("allow_anonymous_public_read must be boolean")
    _optional_safe_id(exact_cross_boundary_approval_id, "exact_cross_boundary_approval_id")
    _optional_safe_id(publication_intent_id, "publication_intent_id")

    if target.access is NestedAccess.WRITE and publication_intent_id is None:
        return _denied(
            target,
            RepositoryAuthFailureCode.NESTED_RESOURCE_DENIED,
            (
                RecoveryAction(
                    RecoveryActionKind.REQUEST_CAPABILITY,
                    (("target_id", target.target_id),),
                ),
                RecoveryAction(RecoveryActionKind.ABORT),
            ),
        )
    if (
        target.owner_boundary != target.primary_owner_boundary
        and exact_cross_boundary_approval_id is None
    ):
        return _denied(
            target,
            RepositoryAuthFailureCode.NESTED_RESOURCE_DENIED,
            (
                RecoveryAction(
                    RecoveryActionKind.REQUEST_CAPABILITY,
                    (("target_id", target.target_id),),
                ),
                RecoveryAction(RecoveryActionKind.ABORT),
            ),
        )
    if target.binding_state is NestedBindingState.EXACT:
        return NestedRoutingDecision(
            status=NestedRoutingStatus.BOUND_PROFILE,
            target_kind=target.target_kind,
            target_id=target.target_id,
            profile_id=target.profile_id,
            capability_ids=target.capability_ids,
            endpoint_digest=target.endpoint_digest,
            failure_code=None,
            recovery_actions=(),
        )
    if target.access is NestedAccess.READ and target.public_read and allow_anonymous_public_read:
        return NestedRoutingDecision(
            status=NestedRoutingStatus.ANONYMOUS_READ,
            target_kind=target.target_kind,
            target_id=target.target_id,
            profile_id=None,
            capability_ids=(),
            endpoint_digest=target.endpoint_digest,
            failure_code=None,
            recovery_actions=(),
        )
    return _denied(
        target,
        RepositoryAuthFailureCode.NESTED_RESOURCE_BINDING_REQUIRED,
        (
            RecoveryAction(
                RecoveryActionKind.RECONCILE_BINDING,
                (("target_id", target.target_id),),
            ),
            RecoveryAction(RecoveryActionKind.ABORT),
        ),
    )


__all__ = [
    "NestedAccess",
    "NestedBindingState",
    "NestedIdentityReceipt",
    "NestedResourceCandidate",
    "NestedResourceKind",
    "NestedResourceTarget",
    "NestedRoutingDecision",
    "NestedRoutingStatus",
    "canonical_nested_endpoint",
    "nested_endpoint_digest",
    "route_nested_resource",
]
