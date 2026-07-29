"""Typed GitHub API identity grants and verification evidence."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from .github_capability_preflight import GitHubOperationCapability
from .repository_auth_broker import EphemeralSecret
from .repository_identity import ActorClass

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_HOST = re.compile(r"^[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?$")
_PERMISSION = re.compile(r"^[a-z][a-z0-9_]{0,63}:(?:read|write|admin)$")


def _safe_id(value: str, field_name: str) -> str:
    if not isinstance(value, str) or _SAFE_ID.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a bounded safe identifier")
    return value


def _host(value: str) -> str:
    if not isinstance(value, str) or _HOST.fullmatch(value) is None:
        raise ValueError("host must be a bounded lowercase host")
    return value


def _unique_ids(values: tuple[str, ...], field_name: str) -> tuple[str, ...]:
    if not isinstance(values, tuple) or len(values) > 64:
        raise ValueError(f"{field_name} must be a bounded tuple")
    normalized = tuple(_safe_id(value, field_name) for value in values)
    if len(set(normalized)) != len(normalized):
        raise ValueError(f"{field_name} entries must be unique")
    return normalized


def _operation_capabilities(values: tuple[str, ...]) -> tuple[str, ...]:
    normalized = _unique_ids(values, "capability_ids")
    if not normalized:
        raise ValueError("capability_ids must be a non-empty tuple")
    try:
        tuple(GitHubOperationCapability(value) for value in normalized)
    except ValueError:
        raise ValueError(
            "capability_ids must contain exact GitHub operation capability IDs"
        ) from None
    return normalized


def _permissions(values: tuple[str, ...]) -> tuple[str, ...]:
    if not isinstance(values, tuple) or len(values) > 64:
        raise ValueError("permission_ids must be a bounded tuple")
    if any(not isinstance(value, str) or _PERMISSION.fullmatch(value) is None for value in values):
        raise ValueError("permission_ids contains an invalid permission")
    if len(set(values)) != len(values):
        raise ValueError("permission_ids entries must be unique")
    return values


def _permission_pairs(values: tuple[tuple[str, str], ...]) -> tuple[tuple[str, str], ...]:
    if not isinstance(values, tuple) or len(values) > 64:
        raise ValueError("permissions must be a bounded tuple")
    keys: list[str] = []
    for name, level in values:
        if (
            not isinstance(name, str)
            or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", name)
            or level not in {"read", "write", "admin"}
        ):
            raise ValueError("permissions contains an invalid entry")
        keys.append(name)
    if len(set(keys)) != len(keys):
        raise ValueError("permission names must be unique")
    return values


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


class GitHubApiIdentityKind(str, Enum):
    STORED_ACCOUNT = "stored_account"
    APP_INSTALLATION = "app_installation"


@dataclass(frozen=True, slots=True)
class StoredGhAccountSpec:
    reference_id: str
    profile_id: str
    host: str
    login: str
    actor_id: str
    actor_class: ActorClass
    repository_id: str
    capability_ids: tuple[str, ...]
    lease_seconds: int = 300

    def __post_init__(self) -> None:
        _safe_id(self.reference_id, "reference_id")
        _safe_id(self.profile_id, "profile_id")
        _host(self.host)
        _safe_id(self.login, "login")
        _safe_id(self.actor_id, "actor_id")
        if (
            not isinstance(self.actor_class, ActorClass)
            or self.actor_class is ActorClass.AUTONOMOUS_AGENT
        ):
            raise ValueError("stored account actor_class must be human-operated or delegated-human")
        _safe_id(self.repository_id, "repository_id")
        _operation_capabilities(self.capability_ids)
        if (
            not isinstance(self.lease_seconds, int)
            or isinstance(self.lease_seconds, bool)
            or not 30 <= self.lease_seconds <= 3_600
        ):
            raise ValueError("lease_seconds must be between 30 and 3600")


@dataclass(frozen=True, slots=True)
class GitHubAppInstallationSpec:
    reference_id: str
    profile_id: str
    host: str
    app_id: str
    installation_id: str
    actor_id: str
    repository_id: str
    capability_ids: tuple[str, ...]
    permissions: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        _safe_id(self.reference_id, "reference_id")
        _safe_id(self.profile_id, "profile_id")
        _host(self.host)
        _safe_id(self.app_id, "app_id")
        _safe_id(self.installation_id, "installation_id")
        _safe_id(self.actor_id, "actor_id")
        _safe_id(self.repository_id, "repository_id")
        _operation_capabilities(self.capability_ids)
        _permission_pairs(self.permissions)
        if not self.permissions:
            raise ValueError("GitHub App installation requires explicit minimal permissions")

    @property
    def permission_ids(self) -> tuple[str, ...]:
        return tuple(f"{name}:{level}" for name, level in self.permissions)


@dataclass(slots=True)
class GitHubApiTokenGrant:
    grant_id: str
    kind: GitHubApiIdentityKind
    token: EphemeralSecret = field(repr=False)
    actor_id: str
    repository_id: str
    capability_ids: tuple[str, ...]
    permission_ids: tuple[str, ...]
    issued_at: str
    expires_at: str
    installation_id: str | None = None
    sso_authorized: bool = True
    approved: bool = True
    revoked: bool = False

    def __post_init__(self) -> None:
        _safe_id(self.grant_id, "grant_id")
        if not isinstance(self.kind, GitHubApiIdentityKind):
            raise ValueError("kind must be a GitHubApiIdentityKind")
        if not isinstance(self.token, EphemeralSecret):
            raise ValueError("token must be an EphemeralSecret")
        _safe_id(self.actor_id, "actor_id")
        _safe_id(self.repository_id, "repository_id")
        _operation_capabilities(self.capability_ids)
        _permissions(self.permission_ids)
        issued = _timestamp(self.issued_at, "issued_at")
        expires = _timestamp(self.expires_at, "expires_at")
        if expires <= issued:
            raise ValueError("expires_at must be later than issued_at")
        if self.installation_id is not None:
            _safe_id(self.installation_id, "installation_id")
        if any(
            not isinstance(value, bool)
            for value in (self.sso_authorized, self.approved, self.revoked)
        ):
            raise ValueError("authorization flags must be boolean")

    def token_digest(self) -> str:
        return hashlib.sha256(self.token.reveal().encode()).hexdigest()

    def safe_payload(self) -> dict[str, object]:
        return {
            "grant_id": self.grant_id,
            "kind": self.kind.value,
            "token_digest": self.token_digest(),
            "actor_id": self.actor_id,
            "repository_id": self.repository_id,
            "capability_ids": list(self.capability_ids),
            "permission_ids": list(self.permission_ids),
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "installation_id": self.installation_id,
            "sso_authorized": self.sso_authorized,
            "approved": self.approved,
            "revoked": self.revoked,
        }


@dataclass(frozen=True, slots=True)
class GitHubApiIdentityProof:
    actor_id: str
    repository_id: str
    capability_ids: tuple[str, ...]
    permission_ids: tuple[str, ...]
    installation_id: str | None = None
    sso_authorized: bool = True
    approved: bool = True
    revoked: bool = False

    def __post_init__(self) -> None:
        _safe_id(self.actor_id, "actor_id")
        _safe_id(self.repository_id, "repository_id")
        _operation_capabilities(self.capability_ids)
        _permissions(self.permission_ids)
        if self.installation_id is not None:
            _safe_id(self.installation_id, "installation_id")
        if any(
            not isinstance(value, bool)
            for value in (self.sso_authorized, self.approved, self.revoked)
        ):
            raise ValueError("authorization flags must be boolean")

    def safe_payload(self) -> dict[str, object]:
        return {
            "actor_id": self.actor_id,
            "repository_id": self.repository_id,
            "capability_ids": list(self.capability_ids),
            "permission_ids": list(self.permission_ids),
            "installation_id": self.installation_id,
            "sso_authorized": self.sso_authorized,
            "approved": self.approved,
            "revoked": self.revoked,
        }
