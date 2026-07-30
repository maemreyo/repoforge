from __future__ import annotations

import json
from dataclasses import replace

import pytest

from repoforge.domain.nested_identity import (
    NestedAccess,
    NestedBindingState,
    NestedIdentityReceipt,
    NestedResourceCandidate,
    NestedResourceKind,
    NestedResourceTarget,
    NestedRoutingStatus,
    canonical_nested_endpoint,
    nested_endpoint_digest,
    route_nested_resource,
)
from repoforge.domain.repository_identity import (
    AuthTargetKind,
    RepositoryAuthFailureCode,
    RepositoryProvider,
)


def _candidate(
    *,
    kind: NestedResourceKind = NestedResourceKind.SUBMODULE,
    access: NestedAccess = NestedAccess.READ,
    endpoint: str = "https://github.com/acme/sdk.git",
    source_location: str = ".gitmodules:vendor/sdk",
    depth: int = 1,
) -> NestedResourceCandidate:
    canonical = canonical_nested_endpoint(endpoint)
    return NestedResourceCandidate(
        kind=kind,
        access=access,
        canonical_endpoint=canonical,
        source_location=source_location,
        depth=depth,
        endpoint_digest=nested_endpoint_digest(canonical),
    )


def _target(
    *,
    kind: NestedResourceKind = NestedResourceKind.SUBMODULE,
    access: NestedAccess = NestedAccess.READ,
    target_kind: AuthTargetKind = AuthTargetKind.SUBMODULE,
    target_id: str = "submodule-654321",
    repository_id: str | None = "654321",
    owner_boundary: str = "company-acme",
    primary_owner_boundary: str = "company-acme",
    capability_ids: tuple[str, ...] = ("git_fetch",),
    binding_state: NestedBindingState = NestedBindingState.EXACT,
    profile_id: str | None = "company-dependency-reader",
    public_read: bool = False,
) -> NestedResourceTarget:
    endpoint = canonical_nested_endpoint("https://github.com/acme/sdk.git")
    return NestedResourceTarget(
        kind=kind,
        access=access,
        provider=RepositoryProvider.GITHUB,
        provider_host="github.com",
        target_kind=target_kind,
        target_id=target_id,
        repository_id=repository_id,
        owner_boundary=owner_boundary,
        primary_owner_boundary=primary_owner_boundary,
        capability_ids=capability_ids,
        endpoint_digest=nested_endpoint_digest(endpoint),
        binding_state=binding_state,
        profile_id=profile_id,
        public_read=public_read,
    )


def test_endpoint_canonicalization_and_digest_are_deterministic() -> None:
    assert canonical_nested_endpoint("HTTPS://GitHub.COM/acme/sdk.git/") == (
        "https://github.com/acme/sdk.git"
    )
    assert (
        canonical_nested_endpoint(
            "../sdk.git", base_endpoint="https://github.com/acme/platform.git"
        )
        == "https://github.com/acme/sdk.git"
    )
    assert canonical_nested_endpoint("git@github.com:acme/sdk.git") == (
        "ssh://git@github.com/acme/sdk.git"
    )
    endpoint = canonical_nested_endpoint("https://github.com/acme/sdk.git")
    assert nested_endpoint_digest(endpoint) == nested_endpoint_digest(endpoint)
    assert len(nested_endpoint_digest(endpoint)) == 64


@pytest.mark.parametrize(
    "endpoint",
    (
        "https://user:secret@github.com/acme/sdk.git",
        "file:///tmp/sdk.git",
        "/tmp/sdk.git",
        "../../sdk.git",
        "ext::sh -c evil",
        "https://github.com/acme/sdk.git\nAuthorization: bearer-secret",
        "ssh://root@github.com/acme/sdk.git",
    ),
)
def test_endpoint_canonicalization_rejects_unsafe_authorities(endpoint: str) -> None:
    with pytest.raises(ValueError):
        canonical_nested_endpoint(endpoint)


@pytest.mark.parametrize(
    ("kind", "access", "target_kind", "capabilities"),
    (
        (
            NestedResourceKind.SUBMODULE,
            NestedAccess.READ,
            AuthTargetKind.SUBMODULE,
            ("git_fetch",),
        ),
        (NestedResourceKind.LFS, NestedAccess.WRITE, AuthTargetKind.LFS, ("lfs_write",)),
        (
            NestedResourceKind.PACKAGE,
            NestedAccess.READ,
            AuthTargetKind.PACKAGE,
            ("package_read",),
        ),
        (
            NestedResourceKind.RELEASE,
            NestedAccess.WRITE,
            AuthTargetKind.RELEASE,
            ("release_write",),
        ),
    ),
)
def test_candidate_and_target_support_every_nested_resource_kind(
    kind: NestedResourceKind,
    access: NestedAccess,
    target_kind: AuthTargetKind,
    capabilities: tuple[str, ...],
) -> None:
    candidate = _candidate(kind=kind, access=access)
    target = _target(
        kind=kind,
        access=access,
        target_kind=target_kind,
        capability_ids=capabilities,
    )
    assert candidate.payload()["kind"] == kind.value
    assert target.payload()["target_kind"] == target_kind.value
    assert target.payload()["capability_ids"] == list(capabilities)


@pytest.mark.parametrize(
    "candidate",
    (
        lambda: replace(_candidate(), depth=-1),
        lambda: replace(_candidate(), depth=33),
        lambda: replace(_candidate(), source_location="../outside"),
        lambda: replace(_candidate(), endpoint_digest="not-a-digest"),
        lambda: replace(_candidate(), canonical_endpoint="file:///tmp/private"),
    ),
)
def test_candidate_rejects_invalid_bounded_discovery_evidence(candidate) -> None:
    with pytest.raises(ValueError):
        candidate()


@pytest.mark.parametrize(
    "target",
    (
        lambda: replace(_target(), capability_ids=("git_fetch", "git_fetch")),
        lambda: replace(_target(), target_kind=AuthTargetKind.PACKAGE),
        lambda: replace(_target(), binding_state=NestedBindingState.EXACT, profile_id=None),
        lambda: replace(
            _target(), binding_state=NestedBindingState.MISSING, profile_id="primary-company"
        ),
        lambda: replace(_target(), public_read=True, access=NestedAccess.WRITE),
        lambda: replace(_target(), endpoint_digest="f" * 63),
    ),
)
def test_target_rejects_ambiguous_or_privilege_broadening_shapes(target) -> None:
    with pytest.raises(ValueError):
        target()


def test_exact_same_boundary_binding_routes_to_its_own_profile() -> None:
    decision = route_nested_resource(
        _target(profile_id="dependency-reader"),
        allow_anonymous_public_read=False,
        exact_cross_boundary_approval_id=None,
        publication_intent_id=None,
    )
    assert decision.status is NestedRoutingStatus.BOUND_PROFILE
    assert decision.profile_id == "dependency-reader"
    assert decision.capability_ids == ("git_fetch",)
    assert decision.failure_code is None


@pytest.mark.parametrize(
    "state",
    (
        NestedBindingState.MISSING,
        NestedBindingState.AMBIGUOUS,
        NestedBindingState.STALE,
        NestedBindingState.DISABLED,
        NestedBindingState.TRANSFERRED,
    ),
)
def test_private_binding_failures_are_typed_and_never_fall_back_to_primary(
    state: NestedBindingState,
) -> None:
    target = _target(binding_state=state, profile_id=None)
    decision = route_nested_resource(
        target,
        allow_anonymous_public_read=False,
        exact_cross_boundary_approval_id=None,
        publication_intent_id=None,
    )
    assert decision.status is NestedRoutingStatus.DENIED
    assert decision.profile_id is None
    assert decision.failure_code is RepositoryAuthFailureCode.NESTED_RESOURCE_BINDING_REQUIRED
    assert all(
        "primary" not in json.dumps(action.payload()).lower()
        for action in decision.recovery_actions
    )


def test_public_read_requires_explicit_anonymous_policy() -> None:
    target = _target(
        repository_id=None,
        binding_state=NestedBindingState.MISSING,
        profile_id=None,
        public_read=True,
    )
    denied = route_nested_resource(
        target,
        allow_anonymous_public_read=False,
        exact_cross_boundary_approval_id=None,
        publication_intent_id=None,
    )
    allowed = route_nested_resource(
        target,
        allow_anonymous_public_read=True,
        exact_cross_boundary_approval_id=None,
        publication_intent_id=None,
    )
    assert denied.status is NestedRoutingStatus.DENIED
    assert allowed.status is NestedRoutingStatus.ANONYMOUS_READ
    assert allowed.profile_id is None
    assert allowed.capability_ids == ()


def test_cross_boundary_and_write_intent_are_exact_fail_closed_gates() -> None:
    cross_boundary = _target(
        access=NestedAccess.WRITE,
        owner_boundary="personal-owner",
        capability_ids=("git_push",),
    )
    no_approval = route_nested_resource(
        cross_boundary,
        allow_anonymous_public_read=False,
        exact_cross_boundary_approval_id=None,
        publication_intent_id="publication-submodule-1",
    )
    no_intent = route_nested_resource(
        cross_boundary,
        allow_anonymous_public_read=False,
        exact_cross_boundary_approval_id="approval-personal-submodule",
        publication_intent_id=None,
    )
    allowed = route_nested_resource(
        cross_boundary,
        allow_anonymous_public_read=False,
        exact_cross_boundary_approval_id="approval-personal-submodule",
        publication_intent_id="publication-submodule-1",
    )
    assert no_approval.failure_code is RepositoryAuthFailureCode.NESTED_RESOURCE_DENIED
    assert no_intent.failure_code is RepositoryAuthFailureCode.NESTED_RESOURCE_DENIED
    assert allowed.status is NestedRoutingStatus.BOUND_PROFILE


def test_receipt_payload_is_target_bound_and_secret_free() -> None:
    receipt = NestedIdentityReceipt(
        target_kind=AuthTargetKind.SUBMODULE,
        target_id="submodule-654321",
        repository_id="654321",
        endpoint_digest="a" * 64,
        routing_status=NestedRoutingStatus.BOUND_PROFILE,
        profile_id="dependency-reader",
        lease_id="lease-submodule-654321",
        capability_ids=("git_fetch",),
        source_locations=(".gitmodules:vendor/sdk", "deps/.gitmodules:sdk"),
    )
    encoded = json.dumps(receipt.payload(), sort_keys=True)
    assert receipt.payload()["lease_id"] == "lease-submodule-654321"
    assert receipt.payload()["source_locations"] == [
        ".gitmodules:vendor/sdk",
        "deps/.gitmodules:sdk",
    ]
    for canary in (
        "ghp_",
        "github_pat_",
        "authorization",
        "private key",
        "credential_ref",
    ):
        assert canary not in encoded.lower()


def test_anonymous_receipt_cannot_claim_profile_lease_or_write_capability() -> None:
    with pytest.raises(ValueError):
        NestedIdentityReceipt(
            target_kind=AuthTargetKind.SUBMODULE,
            target_id="submodule-public",
            repository_id=None,
            endpoint_digest="b" * 64,
            routing_status=NestedRoutingStatus.ANONYMOUS_READ,
            profile_id="primary-company",
            lease_id="lease-primary",
            capability_ids=("git_push",),
            source_locations=(".gitmodules:public",),
        )
