from __future__ import annotations

import json

import pytest

from repoforge.domain.auth_profile import (
    AuthProfileSelector,
    AuthSelectionRequest,
    RequestedActorClass,
    resolve_auth_profile,
)
from repoforge.domain.durable_state import Revision
from repoforge.domain.repository_identity import (
    ActorClass,
    CredentialKind,
    CredentialProfile,
    OpaqueCredentialReference,
    RecoveryActionKind,
    RepositoryAuthFailureCode,
    RepositoryIdentityBinding,
    RepositoryProvider,
)
from repoforge.domain.repository_identity_resolution import (
    CredentialProfileEligibility,
    CredentialRole,
    RepositoryBindingSnapshot,
    RepositoryIdentityObservation,
    RepositoryResolutionOutcome,
)

_SHA_A = "a" * 64
_SHA_B = "b" * 64


def _profile(
    profile_id: str = "personal",
    *,
    actor_class: ActorClass = ActorClass.HUMAN_OPERATED,
    provider: RepositoryProvider = RepositoryProvider.GITHUB,
) -> CredentialProfile:
    return CredentialProfile(
        profile_id=profile_id,
        provider=provider,
        credential_kind=CredentialKind.STORED_ACCOUNT,
        credential_ref=OpaqueCredentialReference(
            scheme="keychain",
            reference_id=f"{profile_id}-credential-v1",
        ),
        actor_class=actor_class,
        expected_actor_id=f"actor-{profile_id}",
        capability_ids=("github_api_read", "git_transport_write"),
        revision=_SHA_A,
    )


def _eligibility(
    profile: CredentialProfile,
    *,
    enabled: bool = True,
    patterns: tuple[str, ...] = ("github.com/maemreyo/*",),
    boundary_id: str | None = None,
) -> CredentialProfileEligibility:
    return CredentialProfileEligibility(
        profile=profile,
        enabled=enabled,
        repository_patterns=patterns,
        boundary_id=boundary_id or f"boundary-{profile.profile_id}",
    )


def _observation() -> RepositoryIdentityObservation:
    return RepositoryIdentityObservation(
        provider=RepositoryProvider.GITHUB,
        provider_host="github.com",
        repository_id="987654",
        canonical_name="github.com/maemreyo/repoforge",
        exists=True,
        observed_at="2026-07-30T12:00:00+00:00",
        config_revision=_SHA_A,
    )


def _binding(
    *,
    human_profile_id: str | None = "personal",
    agent_profile_id: str | None = "automation",
) -> RepositoryIdentityBinding:
    return RepositoryIdentityBinding(
        provider=RepositoryProvider.GITHUB,
        provider_host="github.com",
        repository_id="987654",
        canonical_name="github.com/maemreyo/repoforge",
        human_profile_id=human_profile_id,
        agent_profile_id=agent_profile_id,
        config_revision=_SHA_A,
    )


def _request(
    *,
    selector: AuthProfileSelector | None = None,
    bindings: tuple[RepositoryBindingSnapshot, ...] = (),
    profiles: tuple[CredentialProfileEligibility, ...] | None = None,
    expected_binding_revision: Revision | None = None,
) -> AuthSelectionRequest:
    return AuthSelectionRequest(
        observation=_observation(),
        selector=(selector if selector is not None else AuthProfileSelector()),
        bindings=bindings,
        profiles=(profiles if profiles is not None else (_eligibility(_profile()),)),
        expected_binding_revision=expected_binding_revision,
    )


def test_selector_defaults_preserve_one_profile_human_compatibility() -> None:
    selector = AuthProfileSelector()

    assert selector.auth_profile == "auto"
    assert selector.actor_class is RequestedActorClass.HUMAN
    assert selector.role is CredentialRole.HUMAN
    assert selector.payload() == {"auth_profile": "auto", "actor_class": "human"}

    resolution = resolve_auth_profile(_request(selector=selector))
    assert resolution.outcome is RepositoryResolutionOutcome.PROPOSAL_REQUIRED
    assert resolution.proposal is not None
    assert resolution.proposal.profile_id == "personal"


def test_actor_class_maps_human_and_agent_to_existing_role_slots() -> None:
    assert AuthProfileSelector(actor_class=RequestedActorClass.HUMAN).role is CredentialRole.HUMAN
    assert AuthProfileSelector(actor_class=RequestedActorClass.AGENT).role is CredentialRole.AGENT

    delegated = _profile("delegate", actor_class=ActorClass.DELEGATED_HUMAN)
    delegated_resolution = resolve_auth_profile(_request(profiles=(_eligibility(delegated),)))
    assert delegated_resolution.outcome is RepositoryResolutionOutcome.PROPOSAL_REQUIRED

    automation = _profile("automation", actor_class=ActorClass.AUTONOMOUS_AGENT)
    agent_resolution = resolve_auth_profile(
        _request(
            selector=AuthProfileSelector(actor_class=RequestedActorClass.AGENT),
            profiles=(_eligibility(automation),),
        )
    )
    assert agent_resolution.outcome is RepositoryResolutionOutcome.PROPOSAL_REQUIRED
    assert agent_resolution.proposal is not None
    assert agent_resolution.proposal.binding.agent_profile_id == "automation"


def test_auto_fails_closed_when_multiple_profiles_are_eligible() -> None:
    resolution = resolve_auth_profile(
        _request(
            profiles=(
                _eligibility(_profile("personal")),
                _eligibility(_profile("company")),
            )
        )
    )

    assert resolution.outcome is RepositoryResolutionOutcome.FAILED
    assert resolution.failure is not None
    assert resolution.failure.code is RepositoryAuthFailureCode.PROFILE_AMBIGUOUS
    assert resolution.failure.recovery_actions[0].kind is RecoveryActionKind.RESELECT_PROFILE


def test_explicit_profile_selects_one_candidate_without_persisting_a_binding() -> None:
    resolution = resolve_auth_profile(
        _request(
            selector=AuthProfileSelector(auth_profile="company"),
            profiles=(
                _eligibility(_profile("personal")),
                _eligibility(_profile("company")),
            ),
        )
    )

    assert resolution.outcome is RepositoryResolutionOutcome.PROPOSAL_REQUIRED
    assert resolution.proposal is not None
    assert resolution.proposal.profile_id == "company"
    assert resolution.binding is None
    assert resolution.binding_revision is None


def test_explicit_profile_honors_exact_binding_and_reviewed_revision() -> None:
    resolution = resolve_auth_profile(
        _request(
            selector=AuthProfileSelector(auth_profile="personal"),
            bindings=(RepositoryBindingSnapshot(_binding(), Revision(7)),),
            expected_binding_revision=Revision(7),
        )
    )

    assert resolution.outcome is RepositoryResolutionOutcome.RESOLVED
    assert resolution.profile is not None
    assert resolution.profile.profile_id == "personal"
    assert resolution.binding_revision == Revision(7)


def test_explicit_profile_cannot_override_the_exact_binding_role_slot() -> None:
    resolution = resolve_auth_profile(
        _request(
            selector=AuthProfileSelector(auth_profile="company"),
            bindings=(RepositoryBindingSnapshot(_binding(), Revision(3)),),
            profiles=(
                _eligibility(_profile("personal")),
                _eligibility(_profile("company")),
            ),
        )
    )

    assert resolution.outcome is RepositoryResolutionOutcome.FAILED
    assert resolution.failure is not None
    assert resolution.failure.code is RepositoryAuthFailureCode.PROFILE_NOT_AUTHORIZED
    assert resolution.failure.recovery_actions[0].kind is RecoveryActionKind.RESELECT_PROFILE
    assert resolution.binding_revision == Revision(3)


@pytest.mark.parametrize(
    ("profiles", "profile_id", "actor_class", "expected_code"),
    [
        ((), "missing", RequestedActorClass.HUMAN, RepositoryAuthFailureCode.PROFILE_NOT_FOUND),
        (
            (_eligibility(_profile("disabled"), enabled=False),),
            "disabled",
            RequestedActorClass.HUMAN,
            RepositoryAuthFailureCode.PROFILE_NOT_AUTHORIZED,
        ),
        (
            (_eligibility(_profile("automation", actor_class=ActorClass.AUTONOMOUS_AGENT)),),
            "automation",
            RequestedActorClass.HUMAN,
            RepositoryAuthFailureCode.PROFILE_NOT_AUTHORIZED,
        ),
        (
            (_eligibility(_profile("outside"), patterns=("github.com/other/*",)),),
            "outside",
            RequestedActorClass.HUMAN,
            RepositoryAuthFailureCode.PROFILE_NOT_AUTHORIZED,
        ),
    ],
)
def test_explicit_profile_rejects_missing_disabled_role_and_boundary_mismatches(
    profiles: tuple[CredentialProfileEligibility, ...],
    profile_id: str,
    actor_class: RequestedActorClass,
    expected_code: RepositoryAuthFailureCode,
) -> None:
    resolution = resolve_auth_profile(
        _request(
            selector=AuthProfileSelector(auth_profile=profile_id, actor_class=actor_class),
            profiles=profiles,
        )
    )

    assert resolution.outcome is RepositoryResolutionOutcome.FAILED
    assert resolution.failure is not None
    assert resolution.failure.code is expected_code


def test_explicit_duplicate_profile_declarations_fail_as_ambiguous() -> None:
    resolution = resolve_auth_profile(
        _request(
            selector=AuthProfileSelector(auth_profile="personal"),
            profiles=(
                _eligibility(_profile("personal"), boundary_id="first"),
                _eligibility(_profile("personal"), boundary_id="second"),
            ),
        )
    )

    assert resolution.failure is not None
    assert resolution.failure.code is RepositoryAuthFailureCode.PROFILE_AMBIGUOUS


def test_selector_and_failure_payloads_are_secret_free_and_typed() -> None:
    with pytest.raises(ValueError, match="auth_profile"):
        AuthProfileSelector(auth_profile="ghp_this_is_not_a_profile_identifier")

    resolution = resolve_auth_profile(
        _request(selector=AuthProfileSelector(auth_profile="missing"), profiles=())
    )
    encoded = json.dumps(resolution.payload(), sort_keys=True)

    assert "credential-v1" not in encoded
    assert "token" not in encoded.lower()
    assert "authorization" not in encoded.lower()
    assert '"kind": "reselect_profile"' in encoded
