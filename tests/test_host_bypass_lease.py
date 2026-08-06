"""Operator-issued host-bypass capability leases (#383) -- domain-level coverage.

End-to-end admission wiring (a lease actually widening workspace_exec's runner
allowlist) is covered in tests/test_workspace_exec.py; this file covers the pure
domain logic in isolation: token minting/hashing, lease status transitions, grant
validation, and lease resolution/scope-matching.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from repoforge.domain.errors import ConfigError, SecurityError
from repoforge.domain.host_bypass_lease import (
    MAX_LEASE_TTL_SECONDS,
    MIN_LEASE_TTL_SECONDS,
    HostBypassLease,
    LeaseStatus,
    hash_lease_token,
    mint_lease_token,
    resolve_active_lease,
    validate_lease_grant,
    validate_lease_id,
    verify_lease_token,
)

_NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _lease(**overrides: object) -> HostBypassLease:
    fields: dict[str, object] = {
        "lease_id": "lease-" + "a" * 24,
        "repository_identity": "demo",
        "checkout_identity": "demo",
        "workspace_kind": "managed_worktree",
        "branch_or_ref": "ai/feature",
        "allowed_effects": ("broad_shell",),
        "host_effect_scope": (),
        "credential_profile_ids": (),
        "granted_by": "local-operator",
        "principal_token_hash": hash_lease_token("raw-token"),
        "config_generation": "1",
        "policy_digest": "digest",
        "issued_at": _NOW,
        "expires_at": _NOW + timedelta(minutes=30),
    }
    fields.update(overrides)
    return HostBypassLease(**fields)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Token minting/hashing/verification
# ---------------------------------------------------------------------------


def test_mint_lease_token_returns_distinct_unpredictable_values() -> None:
    raw1, hash1 = mint_lease_token()
    raw2, hash2 = mint_lease_token()
    assert raw1 != raw2
    assert hash1 != hash2
    assert hash1 == hash_lease_token(raw1)


def test_verify_lease_token_accepts_the_matching_raw_value() -> None:
    raw, digest = mint_lease_token()
    assert verify_lease_token(raw, digest) is True


def test_verify_lease_token_rejects_a_wrong_value() -> None:
    _, digest = mint_lease_token()
    assert verify_lease_token("not-the-token", digest) is False


def test_verify_lease_token_rejects_empty_input() -> None:
    _, digest = mint_lease_token()
    assert verify_lease_token("", digest) is False


def test_hash_lease_token_rejects_empty_string() -> None:
    with pytest.raises(ValueError):
        hash_lease_token("")


# ---------------------------------------------------------------------------
# HostBypassLease validation and status
# ---------------------------------------------------------------------------


def test_validate_lease_id_accepts_the_expected_shape() -> None:
    assert validate_lease_id("lease-" + "0" * 24) == "lease-" + "0" * 24


def test_validate_lease_id_rejects_wrong_shape() -> None:
    with pytest.raises(ValueError):
        validate_lease_id("not-a-lease-id")


def test_lease_rejects_naive_datetimes() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        _lease(issued_at=datetime(2026, 1, 1), expires_at=_NOW + timedelta(minutes=30))


def test_lease_rejects_expiry_not_after_issuance() -> None:
    with pytest.raises(ValueError, match="expires_at must be after issued_at"):
        _lease(issued_at=_NOW, expires_at=_NOW)


def test_lease_rejects_duplicate_effects() -> None:
    with pytest.raises(ValueError, match="duplicates"):
        _lease(allowed_effects=("broad_shell", "broad_shell"))


def test_lease_status_active_within_window() -> None:
    lease = _lease()
    assert lease.status(now=_NOW + timedelta(minutes=10)) is LeaseStatus.ACTIVE
    assert lease.is_active(now=_NOW + timedelta(minutes=10)) is True


def test_lease_status_expired_after_window() -> None:
    lease = _lease()
    assert lease.status(now=_NOW + timedelta(hours=1)) is LeaseStatus.EXPIRED
    assert lease.is_active(now=_NOW + timedelta(hours=1)) is False


def test_lease_status_revoked_takes_precedence_over_active_window() -> None:
    lease = _lease().revoke(at=_NOW + timedelta(minutes=5))
    assert lease.status(now=_NOW + timedelta(minutes=10)) is LeaseStatus.REVOKED


def test_revoke_before_expiry_still_blocks_new_use_but_not_retroactively() -> None:
    lease = _lease()
    revoked = lease.revoke(at=_NOW + timedelta(minutes=5))
    # Before the revocation timestamp, the lease was genuinely active -- revoke()
    # does not rewrite history, it only bounds future use.
    assert revoked.status(now=_NOW + timedelta(minutes=1)) is LeaseStatus.ACTIVE
    assert revoked.status(now=_NOW + timedelta(minutes=10)) is LeaseStatus.REVOKED


# ---------------------------------------------------------------------------
# validate_lease_grant
# ---------------------------------------------------------------------------


def test_validate_lease_grant_rejects_a_protected_branch() -> None:
    with pytest.raises(SecurityError, match="protected branch"):
        validate_lease_grant(
            branch_or_ref="main",
            protected_branches=("main",),
            requested_ttl_seconds=1_800,
            max_ttl_seconds=MAX_LEASE_TTL_SECONDS,
        )


def test_validate_lease_grant_accepts_a_non_protected_branch() -> None:
    ttl = validate_lease_grant(
        branch_or_ref="ai/feature",
        protected_branches=("main",),
        requested_ttl_seconds=1_800,
        max_ttl_seconds=MAX_LEASE_TTL_SECONDS,
    )
    assert ttl == 1_800


def test_validate_lease_grant_rejects_ttl_below_minimum() -> None:
    with pytest.raises(ConfigError):
        validate_lease_grant(
            branch_or_ref="ai/feature",
            protected_branches=(),
            requested_ttl_seconds=MIN_LEASE_TTL_SECONDS - 1,
            max_ttl_seconds=MAX_LEASE_TTL_SECONDS,
        )


def test_validate_lease_grant_rejects_ttl_above_repository_ceiling() -> None:
    with pytest.raises(ConfigError, match="ceiling"):
        validate_lease_grant(
            branch_or_ref="ai/feature",
            protected_branches=(),
            requested_ttl_seconds=3_600,
            max_ttl_seconds=1_800,
        )


def test_validate_lease_grant_rejects_ttl_above_global_maximum_even_if_repo_ceiling_is_higher() -> (
    None
):
    with pytest.raises(ConfigError, match="ceiling"):
        validate_lease_grant(
            branch_or_ref="ai/feature",
            protected_branches=(),
            requested_ttl_seconds=MAX_LEASE_TTL_SECONDS + 1,
            max_ttl_seconds=MAX_LEASE_TTL_SECONDS + 10_000,
        )


# ---------------------------------------------------------------------------
# resolve_active_lease -- the AC3 "cannot be replayed" scope-matching logic
# ---------------------------------------------------------------------------


def test_resolve_active_lease_finds_the_matching_lease() -> None:
    raw, digest = mint_lease_token()
    lease = _lease(principal_token_hash=digest)
    resolved = resolve_active_lease(
        [lease],
        raw_token=raw,
        repository_identity="demo",
        checkout_identity="demo",
        branch_or_ref="ai/feature",
        now=_NOW + timedelta(minutes=10),
    )
    assert resolved is lease


def test_resolve_active_lease_returns_none_for_no_token() -> None:
    _, digest = mint_lease_token()
    lease = _lease(principal_token_hash=digest)
    assert (
        resolve_active_lease(
            [lease],
            raw_token="",
            repository_identity="demo",
            checkout_identity="demo",
            branch_or_ref="ai/feature",
        )
        is None
    )


def test_resolve_active_lease_rejects_a_different_repository() -> None:
    raw, digest = mint_lease_token()
    lease = _lease(principal_token_hash=digest, repository_identity="demo")
    resolved = resolve_active_lease(
        [lease],
        raw_token=raw,
        repository_identity="other-repo",
        checkout_identity="demo",
        branch_or_ref="ai/feature",
        now=_NOW + timedelta(minutes=10),
    )
    assert resolved is None


def test_resolve_active_lease_rejects_a_different_checkout() -> None:
    """AC3: a lease minted for one checkout (e.g. one worktree) must not widen
    admission for a different checkout of the same repository and branch."""
    raw, digest = mint_lease_token()
    lease = _lease(principal_token_hash=digest, checkout_identity="demo")
    resolved = resolve_active_lease(
        [lease],
        raw_token=raw,
        repository_identity="demo",
        checkout_identity="a-different-checkout",
        branch_or_ref="ai/feature",
        now=_NOW + timedelta(minutes=10),
    )
    assert resolved is None


def test_resolve_active_lease_rejects_a_different_branch() -> None:
    raw, digest = mint_lease_token()
    lease = _lease(principal_token_hash=digest, branch_or_ref="ai/feature")
    resolved = resolve_active_lease(
        [lease],
        raw_token=raw,
        repository_identity="demo",
        checkout_identity="demo",
        branch_or_ref="ai/some-other-branch",
        now=_NOW + timedelta(minutes=10),
    )
    assert resolved is None


def test_resolve_active_lease_rejects_an_expired_lease() -> None:
    raw, digest = mint_lease_token()
    lease = _lease(principal_token_hash=digest)
    resolved = resolve_active_lease(
        [lease],
        raw_token=raw,
        repository_identity="demo",
        checkout_identity="demo",
        branch_or_ref="ai/feature",
        now=_NOW + timedelta(hours=1),
    )
    assert resolved is None


def test_resolve_active_lease_rejects_a_revoked_lease() -> None:
    raw, digest = mint_lease_token()
    lease = _lease(principal_token_hash=digest).revoke(at=_NOW + timedelta(minutes=1))
    resolved = resolve_active_lease(
        [lease],
        raw_token=raw,
        repository_identity="demo",
        checkout_identity="demo",
        branch_or_ref="ai/feature",
        now=_NOW + timedelta(minutes=10),
    )
    assert resolved is None


def test_resolve_active_lease_rejects_a_wrong_token() -> None:
    _, digest = mint_lease_token()
    lease = _lease(principal_token_hash=digest)
    resolved = resolve_active_lease(
        [lease],
        raw_token="a-completely-different-token",
        repository_identity="demo",
        checkout_identity="demo",
        branch_or_ref="ai/feature",
        now=_NOW + timedelta(minutes=10),
    )
    assert resolved is None


def test_resolve_active_lease_picks_the_right_one_among_several() -> None:
    """AC3, concretely: a token minted for one lease must never resolve a different
    lease, even when several exist for the same repository."""
    _, digest_a = mint_lease_token()
    raw_b, digest_b = mint_lease_token()
    lease_a = _lease(
        lease_id="lease-" + "a" * 24,
        principal_token_hash=digest_a,
        branch_or_ref="ai/feature-a",
    )
    lease_b = _lease(
        lease_id="lease-" + "b" * 24,
        principal_token_hash=digest_b,
        branch_or_ref="ai/feature-b",
    )
    resolved = resolve_active_lease(
        [lease_a, lease_b],
        raw_token=raw_b,
        repository_identity="demo",
        checkout_identity="demo",
        branch_or_ref="ai/feature-b",
        now=_NOW + timedelta(minutes=10),
    )
    assert resolved is lease_b
    # The same token must not resolve against the OTHER lease's branch, even though
    # both leases exist in the same repository.
    assert (
        resolve_active_lease(
            [lease_a, lease_b],
            raw_token=raw_b,
            repository_identity="demo",
            checkout_identity="demo",
            branch_or_ref="ai/feature-a",
            now=_NOW + timedelta(minutes=10),
        )
        is None
    )
