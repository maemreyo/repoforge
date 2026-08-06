"""Operator-issued host-bypass capability leases (#383).

A `trusted_host` lease is the runtime authorization instance the autonomy policy model
(``docs/architecture/autonomy-policy-model.md`` §7) already named: scoped by repository,
checkout, branch/ref, effects, environment, credentials, and TTL, distinct from the
static per-repository capability configuration (whether `trusted_host` is permitted at
all -- see ``RepositoryConfig.trusted_host_enabled``). This module defines the ephemeral
lease record itself; it does not decide whether a repository may issue one at all, and
it does not implement the admission wiring that consumes one (see
``application/workspace/run_adhoc.py``).

Principal binding (#383 AC4, corrected from v3's ``actor_or_session_binding`` per §7):
no durable per-connector/session identity exists anywhere in this codebase to bind to,
so a lease is bound to an opaque bearer token minted at grant time. Only a salted hash
of that token is ever persisted or logged (:func:`hash_lease_token`) -- the raw value is
shown to the operator exactly once, at `rf trust grant` time, and never stored.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum

from .errors import ConfigError, SecurityError

HOST_BYPASS_LEASE_SCHEMA_VERSION = 1

#: Settled in the autonomy policy model §7: 30 min default, 4h max, no sliding or
#: model-initiated renewal. Operator renewal creates a new lease instance.
DEFAULT_LEASE_TTL_SECONDS = 1_800
MAX_LEASE_TTL_SECONDS = 14_400
MIN_LEASE_TTL_SECONDS = 60

_LEASE_ID = re.compile(r"^lease-[a-f0-9]{24}$")
_TOKEN_HASH = re.compile(r"^[a-f0-9]{64}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/#-]{0,127}$")
_MAX_EFFECTS = 32


def validate_lease_id(value: str) -> str:
    if not isinstance(value, str) or _LEASE_ID.fullmatch(value) is None:
        raise ValueError("lease id must use lease-<24 hex>")
    return value


def _identifier(name: str, value: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"{name} is invalid")
    return value


def _effects(name: str, values: tuple[str, ...]) -> tuple[str, ...]:
    if not isinstance(values, tuple) or len(values) > _MAX_EFFECTS:
        raise ValueError(f"{name} must be a tuple of at most {_MAX_EFFECTS} entries")
    for value in values:
        _identifier(name, value)
    if len(set(values)) != len(values):
        raise ValueError(f"{name} contains duplicates")
    return values


class LeaseStatus(str, Enum):
    ACTIVE = "active"
    EXPIRED = "expired"
    REVOKED = "revoked"


def mint_lease_token() -> tuple[str, str]:
    """Return ``(raw_token, token_hash)``. The raw value is never persisted -- the
    caller must show it to the operator once and discard it."""
    raw = secrets.token_urlsafe(32)
    return raw, hash_lease_token(raw)


def hash_lease_token(raw_token: str) -> str:
    if not isinstance(raw_token, str) or not raw_token:
        raise ValueError("lease token must be a non-empty string")
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def verify_lease_token(raw_token: str, expected_hash: str) -> bool:
    """Constant-time comparison -- a lease token is a bearer secret, and comparing
    hashes with ``==`` would leak timing information about how much of it matched."""
    if not isinstance(raw_token, str) or not raw_token:
        return False
    return hmac.compare_digest(hash_lease_token(raw_token), expected_hash)


@dataclass(frozen=True, slots=True)
class HostBypassLease:
    """One ephemeral `trusted_host` lease instance (autonomy policy model §7's schema).

    ``allowed_effects`` and ``host_effect_scope`` are separate: the former is what the
    admission gate widens (broad-shell reach beyond the ordinary allowlist, per AC1),
    the latter is a subset of the categories §14's host-effect matrix marks
    `lease-gated` -- named here as a durable record of what the operator granted, even
    though no admission path consumes it yet (§14's own honesty note: nothing enforces
    those categories until a real containment backend exists, #384).
    """

    lease_id: str
    repository_identity: str
    checkout_identity: str
    workspace_kind: str
    branch_or_ref: str
    allowed_effects: tuple[str, ...]
    host_effect_scope: tuple[str, ...]
    credential_profile_ids: tuple[str, ...]
    granted_by: str
    principal_token_hash: str
    config_generation: str
    policy_digest: str
    issued_at: datetime
    expires_at: datetime
    revoked_at: datetime | None = None
    execution_environment_id: str | None = None

    def __post_init__(self) -> None:
        validate_lease_id(self.lease_id)
        _identifier("repository_identity", self.repository_identity)
        _identifier("checkout_identity", self.checkout_identity)
        _identifier("workspace_kind", self.workspace_kind)
        _identifier("branch_or_ref", self.branch_or_ref)
        _effects("allowed_effects", self.allowed_effects)
        _effects("host_effect_scope", self.host_effect_scope)
        _effects("credential_profile_ids", self.credential_profile_ids)
        _identifier("granted_by", self.granted_by)
        if (
            not isinstance(self.principal_token_hash, str)
            or _TOKEN_HASH.fullmatch(self.principal_token_hash) is None
        ):
            raise ValueError("principal_token_hash must be a sha256 hex digest")
        _identifier("config_generation", self.config_generation)
        _identifier("policy_digest", self.policy_digest)
        if self.issued_at.tzinfo is None or self.expires_at.tzinfo is None:
            raise ValueError("issued_at and expires_at must be timezone-aware")
        if self.expires_at <= self.issued_at:
            raise ValueError("expires_at must be after issued_at")
        if self.revoked_at is not None and self.revoked_at.tzinfo is None:
            raise ValueError("revoked_at must be timezone-aware")

    def status(self, *, now: datetime | None = None) -> LeaseStatus:
        current = now if now is not None else datetime.now(timezone.utc)
        if self.revoked_at is not None and current >= self.revoked_at:
            return LeaseStatus.REVOKED
        if current >= self.expires_at:
            return LeaseStatus.EXPIRED
        return LeaseStatus.ACTIVE

    def is_active(self, *, now: datetime | None = None) -> bool:
        return self.status(now=now) is LeaseStatus.ACTIVE

    def revoke(self, *, at: datetime) -> HostBypassLease:
        if at.tzinfo is None:
            raise ValueError("revoked_at must be timezone-aware")
        return replace(self, revoked_at=at)


def validate_lease_grant(
    *,
    branch_or_ref: str,
    protected_branches: tuple[str, ...],
    requested_ttl_seconds: int,
    max_ttl_seconds: int,
) -> int:
    """Validate a requested grant before a lease is minted, returning the effective
    (bounded) TTL in seconds. Raises before any token is generated or persisted.

    A lease naming a protected branch is rejected outright (#383 scope: "conflict with
    protected-branch policy") -- a `trusted_host` lease widens execution reach, it does
    not create a second path to the higher-order override authority §6/#375 owns for
    protected-ref writes.
    """
    if branch_or_ref in protected_branches:
        raise SecurityError(
            f"Cannot grant a trusted_host lease scoped to protected branch {branch_or_ref!r}",
            unchanged_state=("No lease was created.",),
            safe_next_action=(
                "Scope the lease to a non-protected branch, or use the separate "
                "operator override authority protected-ref writes require."
            ),
        )
    if (
        not isinstance(requested_ttl_seconds, int)
        or isinstance(requested_ttl_seconds, bool)
        or requested_ttl_seconds < MIN_LEASE_TTL_SECONDS
    ):
        raise ConfigError(
            f"Lease TTL must be an integer of at least {MIN_LEASE_TTL_SECONDS} seconds"
        )
    effective_max = min(max_ttl_seconds, MAX_LEASE_TTL_SECONDS)
    if requested_ttl_seconds > effective_max:
        raise ConfigError(
            f"Lease TTL {requested_ttl_seconds}s exceeds the {effective_max}s ceiling "
            "(repository configuration or the global 4h maximum, whichever is lower)"
        )
    return requested_ttl_seconds


def resolve_active_lease(
    leases: Iterable[HostBypassLease],
    *,
    raw_token: str,
    repository_identity: str,
    checkout_identity: str,
    branch_or_ref: str,
    now: datetime | None = None,
) -> HostBypassLease | None:
    """Find the one active lease ``raw_token`` authenticates, scoped to exactly this
    repository, checkout, and branch (#383 AC3: "cannot be replayed against another
    checkout, repository, environment, or broader effect").

    Deliberately does not distinguish "no token presented" from "token presented but
    invalid": both return ``None``, and the caller falls back to ordinary admission --
    a lease can only ever widen what already-admitted execution allows, never replace
    the requirement to fail closed when it doesn't resolve.
    """
    if not raw_token:
        return None
    current = now if now is not None else datetime.now(timezone.utc)
    for lease in leases:
        if (
            lease.repository_identity == repository_identity
            and lease.checkout_identity == checkout_identity
            and lease.branch_or_ref == branch_or_ref
            and lease.is_active(now=current)
            and verify_lease_token(raw_token, lease.principal_token_hash)
        ):
            return lease
    return None


__all__ = [
    "DEFAULT_LEASE_TTL_SECONDS",
    "HOST_BYPASS_LEASE_SCHEMA_VERSION",
    "MAX_LEASE_TTL_SECONDS",
    "MIN_LEASE_TTL_SECONDS",
    "HostBypassLease",
    "LeaseStatus",
    "hash_lease_token",
    "mint_lease_token",
    "resolve_active_lease",
    "validate_lease_grant",
    "validate_lease_id",
    "verify_lease_token",
]
