from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from repoforge.domain.repository_identity import (
    ActorClass,
    AuthLease,
    AuthLeaseState,
    AuthTargetKind,
    CredentialKind,
    CredentialProfile,
    IdentityEvidenceKind,
    IdentityReceipt,
    IdentitySurface,
    IdentitySurfaceEvidence,
    OpaqueCredentialReference,
    OperationIdentityContext,
    PublicationIntent,
    PublicationKind,
    RecoveryAction,
    RecoveryActionKind,
    RepositoryAuthFailure,
    RepositoryAuthFailureCode,
    RepositoryIdentityBinding,
    RepositoryProvider,
    identity_receipt_payload,
)

ROOT = Path(__file__).parents[1]
PACKAGE = ROOT / "src" / "repoforge"
INVENTORY = ROOT / "docs" / "development" / "REPOSITORY_IDENTITY_SURFACES.json"
_SHA256_A = "a" * 64
_SHA256_B = "b" * 64
_SHA40 = "c" * 40


def _reference(name: str = "github-personal-v1") -> OpaqueCredentialReference:
    return OpaqueCredentialReference(scheme="keychain", reference_id=name)


def _profile() -> CredentialProfile:
    return CredentialProfile(
        profile_id="personal",
        provider=RepositoryProvider.GITHUB,
        credential_kind=CredentialKind.STORED_ACCOUNT,
        credential_ref=_reference(),
        actor_class=ActorClass.HUMAN_OPERATED,
        expected_actor_id="github-user-123",
        capability_ids=("github_api_read", "git_transport_write"),
        revision=_SHA256_A,
    )


def _lease(
    *,
    lease_id: str = "lease-personal-repo",
    target_kind: AuthTargetKind = AuthTargetKind.REPOSITORY,
    target_id: str = "github-repository-987654",
    repository_id: str = "987654",
) -> AuthLease:
    return AuthLease(
        lease_id=lease_id,
        profile_id="personal",
        provider=RepositoryProvider.GITHUB,
        repository_id=repository_id,
        target_kind=target_kind,
        target_id=target_id,
        actor_id="github-user-123",
        credential_ref=_reference(),
        issued_at="2026-07-27T10:00:00+00:00",
        expires_at="2026-07-27T11:00:00+00:00",
        state=AuthLeaseState.ACTIVE,
        config_revision=_SHA256_A,
        policy_revision=_SHA256_B,
    )


def test_profile_and_repository_binding_round_trip_safe_metadata() -> None:
    profile = _profile()
    binding = RepositoryIdentityBinding(
        provider=RepositoryProvider.GITHUB,
        repository_id="987654",
        canonical_name="github.com/maemreyo/repoforge",
        human_profile_id="personal",
        agent_profile_id="personal-app",
        config_revision=_SHA256_A,
    )

    encoded = json.dumps(
        {"profile": profile.payload(), "binding": binding.payload()},
        sort_keys=True,
    )
    decoded = json.loads(encoded)

    assert decoded["profile"]["credential_ref"] == {
        "reference_id": "github-personal-v1",
        "scheme": "keychain",
    }
    assert decoded["binding"]["repository_id"] == "987654"
    assert decoded["binding"]["canonical_name"] == "github.com/maemreyo/repoforge"
    assert "token" not in encoded.lower()
    assert "private_key" not in encoded.lower()
    assert "authorization" not in encoded.lower()


@pytest.mark.parametrize(
    "reference_id",
    [
        "ghp_abcdefghijklmnopqrstuvwxyz0123456789",
        "github_pat_abcdefghijklmnopqrstuvwxyz0123456789",
        "Bearer-abcdefghijklmnopqrstuvwxyz0123456789",
        "private-key-material",
    ],
)
def test_opaque_credential_reference_rejects_raw_secret_shapes(reference_id: str) -> None:
    with pytest.raises(ValueError, match="opaque credential reference"):
        OpaqueCredentialReference(scheme="keychain", reference_id=reference_id)


def test_operation_context_supports_multiple_target_bound_leases() -> None:
    repository_lease = _lease()
    package_lease = _lease(
        lease_id="lease-company-package",
        target_kind=AuthTargetKind.PACKAGE,
        target_id="ghcr.io/cicdata-io/private-image",
        repository_id="123456",
    )
    context = OperationIdentityContext(
        operation_id="op-0123456789abcdef01234567",
        primary_repository_id="987654",
        actor_class=ActorClass.AUTONOMOUS_AGENT,
        auth_leases=(repository_lease, package_lease),
        selected_at="2026-07-27T10:00:00+00:00",
        config_revision=_SHA256_A,
        policy_revision=_SHA256_B,
    )

    payload = context.payload()

    assert [item["target_kind"] for item in payload["auth_leases"]] == [
        "repository",
        "package",
    ]
    assert {item["repository_id"] for item in payload["auth_leases"]} == {
        "987654",
        "123456",
    }


def test_publication_intent_binds_exact_source_destination_and_commit() -> None:
    intent = PublicationIntent(
        publication_id="publication-0123456789abcdef01234567",
        operation_id="op-0123456789abcdef01234567",
        kind=PublicationKind.PULL_REQUEST,
        source_repository_id="987654",
        destination_repository_id="987654",
        remote_name="origin",
        source_ref="refs/heads/ai/epic-284-repository-identity",
        destination_ref="refs/heads/ai/epic-284-repository-identity",
        base_ref="refs/heads/main",
        head_ref="refs/heads/ai/epic-284-repository-identity",
        expected_commit_sha=_SHA40,
        cross_boundary_approval_id=None,
    )

    assert intent.exact_refspec == (
        "refs/heads/ai/epic-284-repository-identity:refs/heads/ai/epic-284-repository-identity"
    )
    assert intent.payload()["expected_commit_sha"] == _SHA40

    with pytest.raises(ValueError, match="cross-boundary"):
        PublicationIntent(
            publication_id="publication-1123456789abcdef01234567",
            operation_id="op-0123456789abcdef01234567",
            kind=PublicationKind.GIT_PUSH,
            source_repository_id="987654",
            destination_repository_id="123456",
            remote_name="origin",
            source_ref="refs/heads/main",
            destination_ref="refs/heads/main",
            expected_commit_sha=_SHA40,
        )


def test_identity_evidence_never_promotes_transport_access_to_actor_proof() -> None:
    transport = IdentitySurfaceEvidence(
        surface=IdentitySurface.GIT_PUSH,
        evidence_kind=IdentityEvidenceKind.TRANSPORT_ACCESS_PROOF,
        repository_id="987654",
        profile_id="personal",
        actor_id=None,
        target="github.com/maemreyo/repoforge",
        observed_at="2026-07-27T10:01:00+00:00",
        evidence_digest=_SHA256_A,
    )
    api_actor = IdentitySurfaceEvidence(
        surface=IdentitySurface.GITHUB_API,
        evidence_kind=IdentityEvidenceKind.VERIFIED_ACTOR,
        repository_id="987654",
        profile_id="personal",
        actor_id="github-user-123",
        target="github.com/maemreyo/repoforge",
        observed_at="2026-07-27T10:01:00+00:00",
        evidence_digest=_SHA256_B,
    )

    assert transport.proves_actor is False
    assert api_actor.proves_actor is True
    with pytest.raises(ValueError, match="verified actor evidence requires actor_id"):
        IdentitySurfaceEvidence(
            surface=IdentitySurface.GITHUB_API,
            evidence_kind=IdentityEvidenceKind.VERIFIED_ACTOR,
            repository_id="987654",
            profile_id="personal",
            actor_id=None,
            target="github.com/maemreyo/repoforge",
            observed_at="2026-07-27T10:01:00+00:00",
            evidence_digest=_SHA256_B,
        )


def test_identity_receipt_serializes_only_safe_provenance_metadata() -> None:
    evidence = IdentitySurfaceEvidence(
        surface=IdentitySurface.GITHUB_API,
        evidence_kind=IdentityEvidenceKind.VERIFIED_ACTOR,
        repository_id="987654",
        profile_id="personal",
        actor_id="github-user-123",
        target="github.com/maemreyo/repoforge",
        observed_at="2026-07-27T10:01:00+00:00",
        evidence_digest=_SHA256_A,
    )
    receipt = IdentityReceipt(
        receipt_id="identity-receipt-0123456789abcdef01234567",
        operation_id="op-0123456789abcdef01234567",
        repository_id="987654",
        actor_class=ActorClass.HUMAN_OPERATED,
        profile_ids=("personal",),
        lease_ids=("lease-personal-repo",),
        evidence=(evidence,),
        publication_intent_id="publication-0123456789abcdef01234567",
        commit_sha=_SHA40,
        tree_sha="d" * 40,
        exact_ref="refs/heads/ai/epic-284-repository-identity",
        config_revision=_SHA256_A,
        policy_revision=_SHA256_B,
        created_at="2026-07-27T10:02:00+00:00",
        outcome="verified",
    )

    payload = identity_receipt_payload(receipt)
    encoded = json.dumps(payload, sort_keys=True)

    assert json.loads(encoded) == payload
    assert payload["evidence"][0]["actor_id"] == "github-user-123"
    assert payload["exact_ref"] == "refs/heads/ai/epic-284-repository-identity"
    for canary in (
        "ghp_abcdefghijklmnopqrstuvwxyz0123456789",
        "github_pat_abcdefghijklmnopqrstuvwxyz0123456789",
        "BEGIN PRIVATE KEY",
        "Authorization:",
    ):
        assert canary not in encoded


def test_failure_taxonomy_and_recovery_actions_cover_required_boundaries() -> None:
    required = {
        RepositoryAuthFailureCode.PROFILE_NOT_FOUND,
        RepositoryAuthFailureCode.PROFILE_AMBIGUOUS,
        RepositoryAuthFailureCode.ACTOR_MISMATCH,
        RepositoryAuthFailureCode.TRANSPORT_PROOF_UNAVAILABLE,
        RepositoryAuthFailureCode.LEASE_EXPIRED,
        RepositoryAuthFailureCode.REMOTE_REWRITE_DETECTED,
        RepositoryAuthFailureCode.PUBLICATION_TARGET_MISMATCH,
        RepositoryAuthFailureCode.NESTED_RESOURCE_DENIED,
    }
    assert required.issubset(set(RepositoryAuthFailureCode))

    failure = RepositoryAuthFailure(
        code=RepositoryAuthFailureCode.ACTOR_MISMATCH,
        message="Selected profile actor does not match the verified API actor.",
        retryable=False,
        recovery_actions=(
            RecoveryAction(
                kind=RecoveryActionKind.RESELECT_PROFILE,
                parameters=(("repository_id", "987654"),),
            ),
            RecoveryAction(kind=RecoveryActionKind.ABORT),
        ),
    )
    payload = failure.payload()

    assert payload["code"] == "ACTOR_MISMATCH"
    assert payload["recovery_actions"][0] == {
        "kind": "reselect_profile",
        "parameters": {"repository_id": "987654"},
    }


def test_repository_identity_domain_has_no_provider_adapter_or_secret_body_fields() -> None:
    path = PACKAGE / "domain" / "repository_identity.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imported: set[str] = set()
    field_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            field_names.add(node.target.id)

    assert not any(
        "adapters" in item or item in {"subprocess", "requests", "httpx"} for item in imported
    )
    assert field_names.isdisjoint(
        {
            "token",
            "access_token",
            "authorization",
            "authorization_header",
            "private_key",
            "secret",
            "password",
            "credential_body",
            "helper_response",
        }
    )


def _identity_sensitive_adapter_paths() -> set[str]:
    executables = {"git", "gh", "ssh", "git-lfs"}
    paths: set[str] = set()
    adapters = PACKAGE / "adapters"
    for path in adapters.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        constants = {
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        }
        imports_subprocess = any(
            (
                isinstance(node, ast.Import)
                and any(alias.name == "subprocess" for alias in node.names)
            )
            or (isinstance(node, ast.ImportFrom) and node.module == "subprocess")
            for node in ast.walk(tree)
        )
        if constants.intersection(executables) or imports_subprocess:
            paths.add(str(path.relative_to(ROOT)))
    return paths


def test_identity_surface_inventory_classifies_every_sensitive_production_adapter() -> None:
    payload = json.loads(INVENTORY.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    entries = payload["entries"]
    by_path = {entry["path"]: entry for entry in entries}

    assert set(by_path) == _identity_sensitive_adapter_paths()
    for path, entry in by_path.items():
        assert path.startswith("src/repoforge/adapters/")
        assert entry["executables"]
        assert entry["surfaces"]
        assert entry["credential_inputs"]
        assert entry["target_inputs"]
        assert entry["current_risk"] in {
            "none_local_only",
            "ambient_or_implicit",
            "shared_executor_boundary",
        }
        assert entry["planned_owner"].startswith("#")
