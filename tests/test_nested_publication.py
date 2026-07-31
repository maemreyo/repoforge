from __future__ import annotations

import json
from dataclasses import replace

import pytest

from repoforge.domain.errors import ErrorCode, RepoForgeError
from repoforge.domain.nested_identity import (
    NestedAccess,
    NestedBindingState,
    NestedResourceKind,
    NestedResourceTarget,
)
from repoforge.domain.nested_publication import (
    NestedPublicationIntent,
    ReviewedNestedPublication,
    revalidate_nested_publication,
    review_nested_publication,
)
from repoforge.domain.operation_identity import LeaseCapabilityRequest
from repoforge.domain.repository_identity import (
    AuthLease,
    AuthLeaseState,
    AuthTargetKind,
    OpaqueCredentialReference,
    RepositoryProvider,
)

_OPERATION_ID = "op-" + "1" * 24
_PUBLICATION_ID = "publication-" + "2" * 24
_ENDPOINT = "3" * 64
_PAYLOAD = "4" * 64
_CAPABILITY = "5" * 64
_PERMISSION = "6" * 64
_CONFIG = "7" * 64
_POLICY = "8" * 64
_NOW = "2026-07-30T02:00:00+00:00"


def _target(
    kind: NestedResourceKind = NestedResourceKind.PACKAGE,
    *,
    target_id: str = "package-acme-widget",
    repository_id: str | None = "654321",
    endpoint_digest: str = _ENDPOINT,
    owner_boundary: str = "company",
    primary_owner_boundary: str = "company",
    binding_state: NestedBindingState = NestedBindingState.EXACT,
    profile_id: str | None = "package-publisher",
    capability_ids: tuple[str, ...] | None = None,
) -> NestedResourceTarget:
    target_kind = {
        NestedResourceKind.PACKAGE: AuthTargetKind.PACKAGE,
        NestedResourceKind.LFS: AuthTargetKind.LFS,
        NestedResourceKind.SUBMODULE: AuthTargetKind.SUBMODULE,
        NestedResourceKind.RELEASE: AuthTargetKind.RELEASE,
    }[kind]
    if capability_ids is None:
        capability_ids = {
            NestedResourceKind.PACKAGE: ("package_write",),
            NestedResourceKind.LFS: ("lfs_write",),
            NestedResourceKind.SUBMODULE: ("git_push",),
            NestedResourceKind.RELEASE: ("release_write",),
        }[kind]
    return NestedResourceTarget(
        kind=kind,
        access=NestedAccess.WRITE,
        provider=RepositoryProvider.GITHUB,
        provider_host="github.com",
        target_kind=target_kind,
        target_id=target_id,
        repository_id=repository_id,
        owner_boundary=owner_boundary,
        primary_owner_boundary=primary_owner_boundary,
        capability_ids=capability_ids,
        endpoint_digest=endpoint_digest,
        binding_state=binding_state,
        profile_id=profile_id,
        public_read=False,
    )


def _intent(
    kind: NestedResourceKind = NestedResourceKind.PACKAGE,
    *,
    destination_target_id: str = "package-acme-widget",
    endpoint_digest: str = _ENDPOINT,
    payload_digest: str = _PAYLOAD,
    capability_digest: str = _CAPABILITY,
    permission_digest: str = _PERMISSION,
    approval_id: str | None = None,
    operation_id: str = _OPERATION_ID,
) -> NestedPublicationIntent:
    return NestedPublicationIntent(
        publication_id=_PUBLICATION_ID,
        operation_id=operation_id,
        resource_kind=kind,
        source_repository_id="123456",
        destination_target_id=destination_target_id,
        endpoint_digest=endpoint_digest,
        payload_digest=payload_digest,
        capability_digest=capability_digest,
        permission_digest=permission_digest,
        cross_boundary_approval_id=approval_id,
    )


def _lease(target: NestedResourceTarget | None = None, **changes: object) -> AuthLease:
    current = target or _target()
    lease = AuthLease(
        lease_id=f"lease-{current.target_id}",
        profile_id=current.profile_id or "invalid-missing-profile",
        provider=current.provider,
        repository_id=current.repository_id or "registry-target",
        target_kind=current.target_kind,
        target_id=current.target_id,
        actor_id="github-app-42",
        credential_ref=OpaqueCredentialReference("github-app", "package-publisher"),
        issued_at="2026-07-30T01:00:00+00:00",
        expires_at="2026-07-30T03:00:00+00:00",
        state=AuthLeaseState.ACTIVE,
        config_revision=_CONFIG,
        policy_revision=_POLICY,
        material_digest="9" * 64,
        provider_metadata=(("nested_endpoint_digest", current.endpoint_digest),),
    )
    return replace(lease, **changes)


def _request(
    target: NestedResourceTarget | None = None, **changes: object
) -> LeaseCapabilityRequest:
    current = target or _target()
    request = LeaseCapabilityRequest(f"lease-{current.target_id}", current.capability_ids)
    return replace(request, **changes)


@pytest.mark.parametrize(
    ("kind", "target_id"),
    (
        (NestedResourceKind.PACKAGE, "package-acme-widget"),
        (NestedResourceKind.LFS, "lfs-acme-widget"),
    ),
)
def test_reviews_exact_package_and_lfs_write_intents(
    kind: NestedResourceKind,
    target_id: str,
) -> None:
    target = _target(kind, target_id=target_id)
    intent = _intent(kind, destination_target_id=target_id)

    reviewed = review_nested_publication(
        intent,
        target=target,
        lease=_lease(target),
        capability_request=_request(target),
        observed_capability_digest=_CAPABILITY,
        observed_permission_digest=_PERMISSION,
        now=_NOW,
    )

    assert isinstance(reviewed, ReviewedNestedPublication)
    assert reviewed.intent == intent
    assert reviewed.lease_id == f"lease-{target_id}"
    assert reviewed.target_kind is target.target_kind
    assert reviewed.target_id == target_id
    assert reviewed.endpoint_digest == _ENDPOINT
    assert reviewed.config_revision == _CONFIG
    assert reviewed.policy_revision == _POLICY
    assert len(reviewed.review_digest) == 64
    rendered = json.dumps(reviewed.safe_payload(), sort_keys=True).lower()
    assert "credential_ref" not in rendered
    assert "token" not in rendered
    assert reviewed.safe_payload()["lease_id"] != reviewed.safe_payload()["publication_id"]


@pytest.mark.parametrize("kind", (NestedResourceKind.SUBMODULE, NestedResourceKind.RELEASE))
def test_nested_publication_intent_rejects_non_package_lfs_kinds(
    kind: NestedResourceKind,
) -> None:
    with pytest.raises(ValueError):
        _intent(kind)


def test_review_requires_exact_write_target_capability_and_lease() -> None:
    target = _target()
    cases = (
        (_target(capability_ids=("package_read",)), _lease(target), _request(target)),
        (target, _lease(target), replace(_request(target), capability_ids=("package_read",))),
        (target, replace(_lease(target), target_id="package-other"), _request(target)),
        (target, replace(_lease(target), profile_id="personal-profile"), _request(target)),
        (
            target,
            replace(
                _lease(target),
                provider_metadata=(("nested_endpoint_digest", "a" * 64),),
            ),
            _request(target),
        ),
    )
    for current_target, lease, capability_request in cases:
        with pytest.raises(RepoForgeError):
            review_nested_publication(
                _intent(),
                target=current_target,
                lease=lease,
                capability_request=capability_request,
                observed_capability_digest=_CAPABILITY,
                observed_permission_digest=_PERMISSION,
                now=_NOW,
            )


def test_review_requires_exact_observed_digests_and_boundary_approval() -> None:
    target = _target(owner_boundary="personal", primary_owner_boundary="company")
    with pytest.raises(RepoForgeError) as approval:
        review_nested_publication(
            _intent(),
            target=target,
            lease=_lease(target),
            capability_request=_request(target),
            observed_capability_digest=_CAPABILITY,
            observed_permission_digest=_PERMISSION,
            now=_NOW,
        )
    assert approval.value.code is ErrorCode.CROSS_BOUNDARY_PUBLICATION_DENIED

    intent = _intent(approval_id="approval-company-personal")
    for capability_digest, permission_digest, code in (
        ("a" * 64, _PERMISSION, ErrorCode.CREDENTIAL_CAPABILITY_DENIED),
        (_CAPABILITY, "b" * 64, ErrorCode.GITHUB_API_PERMISSION_DENIED),
    ):
        with pytest.raises(RepoForgeError) as mismatch:
            review_nested_publication(
                intent,
                target=target,
                lease=_lease(target),
                capability_request=_request(target),
                observed_capability_digest=capability_digest,
                observed_permission_digest=permission_digest,
                now=_NOW,
            )
        assert mismatch.value.code is code


def test_review_rejects_expired_revoked_and_non_exact_binding() -> None:
    target = _target()
    cases = (
        (
            _lease(target, expires_at="2026-07-30T01:30:00+00:00"),
            target,
            ErrorCode.CREDENTIAL_EXPIRED,
        ),
        (_lease(target, state=AuthLeaseState.REVOKED), target, ErrorCode.CREDENTIAL_REVOKED),
        (
            _lease(target),
            _target(binding_state=NestedBindingState.TRANSFERRED, profile_id=None),
            ErrorCode.PUBLICATION_TARGET_MISMATCH,
        ),
    )
    for lease, current_target, code in cases:
        with pytest.raises(RepoForgeError) as failure:
            review_nested_publication(
                _intent(),
                target=current_target,
                lease=lease,
                capability_request=_request(target),
                observed_capability_digest=_CAPABILITY,
                observed_permission_digest=_PERMISSION,
                now=_NOW,
            )
        assert failure.value.code is code


@pytest.mark.parametrize(
    "drift",
    (
        "operation",
        "target",
        "transfer",
        "endpoint",
        "capability",
        "permission",
        "payload",
        "approval",
        "config",
        "policy",
    ),
)
def test_revalidation_rejects_all_identity_and_payload_drift(drift: str) -> None:
    target = _target(owner_boundary="personal", primary_owner_boundary="company")
    intent = _intent(approval_id="approval-company-personal")
    lease = _lease(target)
    request = _request(target)
    reviewed = review_nested_publication(
        intent,
        target=target,
        lease=lease,
        capability_request=request,
        observed_capability_digest=_CAPABILITY,
        observed_permission_digest=_PERMISSION,
        now=_NOW,
    )
    current_intent = intent
    current_target = target
    current_lease = lease
    current_request = request
    observed_capability = _CAPABILITY
    observed_permission = _PERMISSION
    if drift == "operation":
        current_intent = replace(intent, operation_id="op-" + "f" * 24)
    elif drift == "target":
        current_intent = replace(intent, destination_target_id="package-other")
        current_target = replace(target, target_id="package-other")
        current_lease = replace(lease, target_id="package-other")
        current_request = replace(request, lease_id=current_lease.lease_id)
    elif drift == "transfer":
        current_target = replace(target, repository_id="999999")
        current_lease = replace(lease, repository_id="999999")
    elif drift == "endpoint":
        current_intent = replace(intent, endpoint_digest="a" * 64)
        current_target = replace(target, endpoint_digest="a" * 64)
        current_lease = replace(
            lease,
            provider_metadata=(("nested_endpoint_digest", "a" * 64),),
        )
    elif drift == "capability":
        observed_capability = "a" * 64
    elif drift == "permission":
        observed_permission = "b" * 64
    elif drift == "payload":
        current_intent = replace(intent, payload_digest="c" * 64)
    elif drift == "approval":
        current_intent = replace(intent, cross_boundary_approval_id="approval-other")
    elif drift == "config":
        current_lease = replace(lease, config_revision="d" * 64)
    else:
        current_lease = replace(lease, policy_revision="e" * 64)

    with pytest.raises(RepoForgeError):
        revalidate_nested_publication(
            reviewed,
            intent=current_intent,
            target=current_target,
            lease=current_lease,
            capability_request=current_request,
            observed_capability_digest=observed_capability,
            observed_permission_digest=observed_permission,
            now="2026-07-30T02:10:00+00:00",
        )


def test_revalidation_of_unchanged_review_is_idempotent() -> None:
    target = _target()
    intent = _intent()
    lease = _lease(target)
    request = _request(target)
    reviewed = review_nested_publication(
        intent,
        target=target,
        lease=lease,
        capability_request=request,
        observed_capability_digest=_CAPABILITY,
        observed_permission_digest=_PERMISSION,
        now=_NOW,
    )

    assert (
        revalidate_nested_publication(
            reviewed,
            intent=intent,
            target=target,
            lease=lease,
            capability_request=request,
            observed_capability_digest=_CAPABILITY,
            observed_permission_digest=_PERMISSION,
            now="2026-07-30T02:10:00+00:00",
        )
        == reviewed
    )
