from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from repoforge.adapters.persistence.json_repository_binding_store import (
    JsonRepositoryBindingStore,
)
from repoforge.bootstrap import AdapterOverrides, build_application
from repoforge.config import AppConfig, ServerConfig
from repoforge.domain.durable_state import Revision
from repoforge.domain.errors import ErrorCode, RepoForgeError
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
from repoforge.domain.repository_identity_resolution import (
    CredentialProfileEligibility,
    CredentialRole,
    RepositoryBindingSnapshot,
    RepositoryIdentityObservation,
    RepositoryReconciliationKind,
    RepositoryResolutionOutcome,
    resolve_repository_identity,
)
from repoforge.testing.fakes import InMemoryLockManager, ScriptedCommandExecutor

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


def _binding(
    *,
    repository_id: str = "987654",
    canonical_name: str = "github.com/maemreyo/repoforge",
    human_profile_id: str | None = "personal",
    agent_profile_id: str | None = "personal-app",
    config_revision: str = _SHA256_A,
    provider_host: str = "github.com",
) -> RepositoryIdentityBinding:
    return RepositoryIdentityBinding(
        provider=RepositoryProvider.GITHUB,
        repository_id=repository_id,
        canonical_name=canonical_name,
        human_profile_id=human_profile_id,
        agent_profile_id=agent_profile_id,
        config_revision=config_revision,
        provider_host=provider_host,
    )


def _observation(
    *,
    repository_id: str = "987654",
    canonical_name: str = "github.com/maemreyo/repoforge",
    exists: bool = True,
    config_revision: str = _SHA256_A,
    provider_host: str = "github.com",
    observed_at: str = "2026-07-27T12:00:00+00:00",
) -> RepositoryIdentityObservation:
    return RepositoryIdentityObservation(
        provider=RepositoryProvider.GITHUB,
        provider_host=provider_host,
        repository_id=repository_id,
        canonical_name=canonical_name,
        exists=exists,
        observed_at=observed_at,
        config_revision=config_revision,
    )


def _eligibility(
    profile: CredentialProfile,
    *,
    enabled: bool = True,
    patterns: tuple[str, ...] = ("github.com/maemreyo/*",),
    boundary_id: str = "personal-owner",
) -> CredentialProfileEligibility:
    return CredentialProfileEligibility(
        profile=profile,
        enabled=enabled,
        repository_patterns=patterns,
        boundary_id=boundary_id,
    )


def _company_profile() -> CredentialProfile:
    return CredentialProfile(
        profile_id="company",
        provider=RepositoryProvider.GITHUB,
        credential_kind=CredentialKind.STORED_ACCOUNT,
        credential_ref=_reference("github-company-v1"),
        actor_class=ActorClass.HUMAN_OPERATED,
        expected_actor_id="github-company-user",
        capability_ids=("github_api_read", "git_transport_write"),
        revision=_SHA256_B,
    )


def _agent_profile() -> CredentialProfile:
    return CredentialProfile(
        profile_id="personal-app",
        provider=RepositoryProvider.GITHUB,
        credential_kind=CredentialKind.GITHUB_APP,
        credential_ref=OpaqueCredentialReference(
            scheme="broker",
            reference_id="github-personal-app-v1",
        ),
        actor_class=ActorClass.AUTONOMOUS_AGENT,
        expected_actor_id="github-app-42",
        capability_ids=("github_api_read", "git_transport_write"),
        revision=_SHA256_B,
    )


def test_exact_binding_survives_same_owner_rename_with_reconciliation_evidence() -> None:
    resolution = resolve_repository_identity(
        observation=_observation(canonical_name="github.com/maemreyo/repoforge-renamed"),
        role=CredentialRole.HUMAN,
        bindings=(RepositoryBindingSnapshot(_binding(), Revision(3)),),
        profiles=(_eligibility(_profile()),),
        expected_binding_revision=Revision(3),
    )

    assert resolution.outcome is RepositoryResolutionOutcome.RESOLVED
    assert resolution.profile == _profile()
    assert resolution.binding_revision == Revision(3)
    assert resolution.reconciliation is not None
    assert resolution.reconciliation.kind is RepositoryReconciliationKind.RENAMED
    assert resolution.reconciliation.previous_canonical_name == "github.com/maemreyo/repoforge"
    assert resolution.evidence.payload()["repository_id"] == "987654"


def test_repository_transfer_across_owner_boundary_fails_closed() -> None:
    resolution = resolve_repository_identity(
        observation=_observation(canonical_name="github.com/cicdata-io/repoforge"),
        role=CredentialRole.HUMAN,
        bindings=(RepositoryBindingSnapshot(_binding(), Revision(1)),),
        profiles=(_eligibility(_profile()),),
    )

    assert resolution.outcome is RepositoryResolutionOutcome.FAILED
    assert resolution.failure is not None
    assert resolution.failure.code is RepositoryAuthFailureCode.BINDING_REPOSITORY_MISMATCH
    assert resolution.failure.recovery_actions[0].kind is RecoveryActionKind.RECONCILE_BINDING


def test_one_eligible_unbound_profile_produces_reviewable_proposal() -> None:
    resolution = resolve_repository_identity(
        observation=_observation(),
        role=CredentialRole.HUMAN,
        bindings=(),
        profiles=(_eligibility(_profile()),),
    )

    assert resolution.outcome is RepositoryResolutionOutcome.PROPOSAL_REQUIRED
    assert resolution.proposal is not None
    assert resolution.proposal.profile_id == "personal"
    assert resolution.proposal.binding == _binding(agent_profile_id=None)
    encoded = json.dumps(resolution.payload(), sort_keys=True)
    assert "github-personal-v1" not in encoded
    assert "token" not in encoded.lower()


def test_unbound_resolution_fails_for_ambiguous_disabled_and_missing_profiles() -> None:
    ambiguous = resolve_repository_identity(
        observation=_observation(),
        role=CredentialRole.HUMAN,
        bindings=(),
        profiles=(
            _eligibility(_profile()),
            _eligibility(_company_profile(), boundary_id="second-personal-profile"),
        ),
    )
    assert ambiguous.failure is not None
    assert ambiguous.failure.code is RepositoryAuthFailureCode.PROFILE_AMBIGUOUS

    disabled = resolve_repository_identity(
        observation=_observation(),
        role=CredentialRole.HUMAN,
        bindings=(),
        profiles=(_eligibility(_profile(), enabled=False),),
    )
    assert disabled.failure is not None
    assert disabled.failure.code is RepositoryAuthFailureCode.PROFILE_NOT_AUTHORIZED

    missing = resolve_repository_identity(
        observation=_observation(),
        role=CredentialRole.HUMAN,
        bindings=(),
        profiles=(),
    )
    assert missing.failure is not None
    assert missing.failure.code is RepositoryAuthFailureCode.PROFILE_NOT_FOUND


def test_delete_recreate_and_stable_id_collision_are_not_silently_rebound() -> None:
    deleted = resolve_repository_identity(
        observation=_observation(exists=False),
        role=CredentialRole.HUMAN,
        bindings=(RepositoryBindingSnapshot(_binding(), Revision(1)),),
        profiles=(_eligibility(_profile()),),
    )
    assert deleted.failure is not None
    assert deleted.failure.code is RepositoryAuthFailureCode.REPOSITORY_BINDING_NOT_FOUND

    recreated = resolve_repository_identity(
        observation=_observation(repository_id="111111"),
        role=CredentialRole.HUMAN,
        bindings=(RepositoryBindingSnapshot(_binding(repository_id="987654"), Revision(1)),),
        profiles=(_eligibility(_profile()),),
    )
    assert recreated.failure is not None
    assert recreated.failure.code is RepositoryAuthFailureCode.BINDING_REPOSITORY_MISMATCH

    collision = resolve_repository_identity(
        observation=_observation(),
        role=CredentialRole.HUMAN,
        bindings=(
            RepositoryBindingSnapshot(_binding(), Revision(1)),
            RepositoryBindingSnapshot(
                _binding(canonical_name="github.com/maemreyo/other"), Revision(2)
            ),
        ),
        profiles=(_eligibility(_profile()),),
    )
    assert collision.failure is not None
    assert collision.failure.code is RepositoryAuthFailureCode.BINDING_AMBIGUOUS


def test_binding_revision_and_selected_role_are_deterministic() -> None:
    stale = resolve_repository_identity(
        observation=_observation(),
        role=CredentialRole.HUMAN,
        bindings=(RepositoryBindingSnapshot(_binding(), Revision(2)),),
        profiles=(_eligibility(_profile()),),
        expected_binding_revision=Revision(1),
    )
    assert stale.failure is not None
    assert stale.failure.code is RepositoryAuthFailureCode.BINDING_STALE

    agent = resolve_repository_identity(
        observation=_observation(),
        role=CredentialRole.AGENT,
        bindings=(RepositoryBindingSnapshot(_binding(), Revision(2)),),
        profiles=(_eligibility(_agent_profile()),),
        expected_binding_revision=Revision(2),
    )
    assert agent.outcome is RepositoryResolutionOutcome.RESOLVED
    assert agent.profile == _agent_profile()


def test_exact_binding_profile_and_config_failures_are_typed() -> None:
    missing_role = resolve_repository_identity(
        observation=_observation(),
        role=CredentialRole.HUMAN,
        bindings=(
            RepositoryBindingSnapshot(
                _binding(human_profile_id=None),
                Revision(1),
            ),
        ),
        profiles=(_eligibility(_profile()),),
    )
    assert missing_role.failure is not None
    assert missing_role.failure.code is RepositoryAuthFailureCode.PROFILE_NOT_FOUND
    assert missing_role.failure.recovery_actions[0].kind is RecoveryActionKind.RESELECT_PROFILE

    duplicate_profile = resolve_repository_identity(
        observation=_observation(),
        role=CredentialRole.HUMAN,
        bindings=(RepositoryBindingSnapshot(_binding(), Revision(1)),),
        profiles=(
            _eligibility(_profile(), boundary_id="profile-entry-one"),
            _eligibility(_profile(), boundary_id="profile-entry-two"),
        ),
    )
    assert duplicate_profile.failure is not None
    assert duplicate_profile.failure.code is RepositoryAuthFailureCode.PROFILE_AMBIGUOUS

    disabled_profile = resolve_repository_identity(
        observation=_observation(),
        role=CredentialRole.HUMAN,
        bindings=(RepositoryBindingSnapshot(_binding(), Revision(1)),),
        profiles=(_eligibility(_profile(), enabled=False),),
    )
    assert disabled_profile.failure is not None
    assert disabled_profile.failure.code is RepositoryAuthFailureCode.PROFILE_NOT_AUTHORIZED
    assert disabled_profile.failure.recovery_actions[0].kind is RecoveryActionKind.REAUTHORIZE

    role_mismatch = resolve_repository_identity(
        observation=_observation(),
        role=CredentialRole.AGENT,
        bindings=(
            RepositoryBindingSnapshot(
                _binding(agent_profile_id="personal"),
                Revision(1),
            ),
        ),
        profiles=(_eligibility(_profile()),),
    )
    assert role_mismatch.failure is not None
    assert role_mismatch.failure.code is RepositoryAuthFailureCode.PROFILE_NOT_AUTHORIZED

    config_stale = resolve_repository_identity(
        observation=_observation(config_revision=_SHA256_B),
        role=CredentialRole.HUMAN,
        bindings=(RepositoryBindingSnapshot(_binding(), Revision(1)),),
        profiles=(_eligibility(_profile()),),
    )
    assert config_stale.failure is not None
    assert config_stale.failure.code is RepositoryAuthFailureCode.BINDING_STALE
    assert config_stale.failure.retryable is True


def test_resolution_metadata_validation_fails_closed() -> None:
    with pytest.raises(ValueError, match="provider_host"):
        _observation(
            provider_host="GitHub.com",
            canonical_name="GitHub.com/maemreyo/repoforge",
        )
    with pytest.raises(ValueError, match="repository_id"):
        _observation(repository_id="invalid/repository")
    with pytest.raises(ValueError, match="host must match"):
        _observation(
            provider_host="github.enterprise.example",
            canonical_name="github.com/maemreyo/repoforge",
        )
    with pytest.raises(ValueError, match="timezone"):
        _observation(observed_at="2026-07-27T12:00:00")
    with pytest.raises(ValueError, match="SHA-256"):
        _observation(config_revision="invalid")
    with pytest.raises(ValueError, match="non-empty tuple"):
        _eligibility(_profile(), patterns=())
    with pytest.raises(ValueError, match="unique"):
        _eligibility(
            _profile(),
            patterns=("github.com/maemreyo/*", "github.com/maemreyo/*"),
        )


def test_personal_and_company_bindings_resolve_concurrently_without_global_state() -> None:
    bindings = (
        RepositoryBindingSnapshot(_binding(), Revision(1)),
        RepositoryBindingSnapshot(
            _binding(
                repository_id="123456",
                canonical_name="github.com/cicdata-io/datanest-recon",
                human_profile_id="company",
                agent_profile_id=None,
                config_revision=_SHA256_B,
            ),
            Revision(4),
        ),
    )
    profiles = (
        _eligibility(_profile()),
        _eligibility(
            _company_profile(),
            patterns=("github.com/cicdata-io/*",),
            boundary_id="company-owner",
        ),
    )

    personal = resolve_repository_identity(
        observation=_observation(),
        role=CredentialRole.HUMAN,
        bindings=bindings,
        profiles=profiles,
    )
    company = resolve_repository_identity(
        observation=_observation(
            repository_id="123456",
            canonical_name="github.com/cicdata-io/datanest-recon",
            config_revision=_SHA256_B,
        ),
        role=CredentialRole.HUMAN,
        bindings=bindings,
        profiles=profiles,
    )

    assert personal.profile is not None and personal.profile.profile_id == "personal"
    assert company.profile is not None and company.profile.profile_id == "company"


def test_discovery_wildcards_must_remain_inside_one_owner_boundary() -> None:
    with pytest.raises(ValueError, match="owner boundary"):
        _eligibility(_profile(), patterns=("github.com/*",))
    with pytest.raises(ValueError, match="owner boundary"):
        _eligibility(_profile(), patterns=("github.com/*/*",))


def test_repository_binding_record_key_rejects_unsafe_identifiers(tmp_path: Path) -> None:
    store = JsonRepositoryBindingStore(tmp_path, InMemoryLockManager())
    with pytest.raises(ValueError, match="provider_host"):
        store.read("GitHub.com", "987654")
    with pytest.raises(ValueError, match="repository_id"):
        store.read("github.com", "invalid/repository")


def test_provider_host_is_part_of_the_stable_binding_key(tmp_path: Path) -> None:
    enterprise_host = "github.enterprise.example"
    enterprise_name = f"{enterprise_host}/platform/repoforge"
    enterprise = _binding(
        canonical_name=enterprise_name,
        provider_host=enterprise_host,
    )
    store = JsonRepositoryBindingStore(tmp_path, InMemoryLockManager())

    public = store.create(_binding())
    private = store.create(enterprise)

    assert public.record_id != private.record_id
    assert store.read("github.com", "987654") == public
    assert store.read(enterprise_host, "987654") == private

    resolution = resolve_repository_identity(
        observation=_observation(
            canonical_name=enterprise_name,
            provider_host=enterprise_host,
        ),
        role=CredentialRole.HUMAN,
        bindings=(RepositoryBindingSnapshot(enterprise, Revision(1)),),
        profiles=(
            _eligibility(
                _profile(),
                patterns=(f"{enterprise_host}/platform/*",),
                boundary_id="enterprise-platform",
            ),
        ),
        expected_binding_revision=Revision(1),
    )
    assert resolution.outcome is RepositoryResolutionOutcome.RESOLVED


def test_json_repository_binding_registry_is_private_restart_safe_and_cas(tmp_path: Path) -> None:
    store = JsonRepositoryBindingStore(tmp_path, InMemoryLockManager())
    created = store.create(_binding())
    assert created.revision == Revision(1)
    assert store.read("github.com", "987654") == created
    assert (
        JsonRepositoryBindingStore(tmp_path, InMemoryLockManager()).read("github.com", "987654")
        == created
    )

    updated_binding = _binding(canonical_name="github.com/maemreyo/repoforge-renamed")
    saved = store.save(updated_binding, expected_revision=Revision(1))
    assert saved.revision == Revision(2)
    with pytest.raises(RepoForgeError) as stale:
        store.save(_binding(), expected_revision=Revision(1))
    assert stale.value.code is ErrorCode.STATE_STALE
    with pytest.raises(RepoForgeError) as duplicate:
        store.create(updated_binding)
    assert duplicate.value.code is ErrorCode.ALREADY_EXISTS

    records = store.list_bindings(max_records=10)
    assert tuple(item.value for item in records.records) == (updated_binding,)
    encoded = next((tmp_path / "repository-identity-bindings").glob("*.json")).read_text(
        encoding="utf-8"
    )
    assert "github-personal-v1" not in encoded
    assert "token" not in encoded.lower()


def test_build_application_wires_repository_binding_registry(tmp_path: Path) -> None:
    config = AppConfig(
        source_path=tmp_path / "config.toml",
        server=ServerConfig(
            workspace_root=tmp_path / "workspaces",
            state_root=tmp_path / "state",
        ),
        repositories={},
    )

    application = build_application(
        config,
        overrides=AdapterOverrides(command=ScriptedCommandExecutor()),
    )

    assert isinstance(application.context.repository_bindings, JsonRepositoryBindingStore)
