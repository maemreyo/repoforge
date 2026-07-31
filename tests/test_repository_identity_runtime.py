from __future__ import annotations

from pathlib import Path

import pytest

from repoforge.adapters.locking import FcntlLockManager
from repoforge.adapters.persistence.json_repository_binding_store import (
    JsonRepositoryBindingStore,
)
from repoforge.application.repository_identity_runtime import RepositoryIdentityRuntime
from repoforge.config import AppConfig, AuthProfileConfig, RepositoryConfig, ServerConfig
from repoforge.domain.auth_profile import AuthProfileSelector, RequestedActorClass
from repoforge.domain.durable_state import Revision
from repoforge.domain.errors import ErrorCode, RepoForgeError
from repoforge.domain.git_transport_identity import (
    GitTransportAccess,
    GitTransportKind,
    GitTransportSpec,
)
from repoforge.domain.github_api_identity import (
    GitHubAppInstallationSpec,
    StoredGhAccountSpec,
)
from repoforge.domain.repository_auth_broker import (
    AuthEnvironmentBinding,
    AuthMaterial,
    AuthMaterialState,
    EphemeralSecret,
    RepositoryAuthBroker,
)
from repoforge.domain.repository_identity import (
    ActorClass,
    AuthTargetKind,
    CredentialKind,
    CredentialProfile,
    OpaqueCredentialReference,
    RepositoryIdentityBinding,
    RepositoryProvider,
)
from repoforge.domain.repository_identity_resolution import (
    CredentialProfileEligibility,
    RepositoryIdentityObservation,
)
from repoforge.testing import FixedClock
from repoforge.testing.auth_fakes import DeterministicAuthMaterialProvider

NOW = "2026-07-31T00:00:00+00:00"
_SHA = "a" * 64
_CAPABILITIES = ("github.contents.read", "github.contents.write")


def _profile_config(
    profile_id: str,
    *,
    repository_id: str,
    repository_name: str,
    actor_class: ActorClass = ActorClass.HUMAN_OPERATED,
) -> AuthProfileConfig:
    reference_id = f"{profile_id}-credential-v1"
    is_agent = actor_class is ActorClass.AUTONOMOUS_AGENT
    profile = CredentialProfile(
        profile_id=profile_id,
        provider=RepositoryProvider.GITHUB,
        credential_kind=CredentialKind.GITHUB_APP if is_agent else CredentialKind.STORED_ACCOUNT,
        credential_ref=OpaqueCredentialReference(
            "github-app" if is_agent else "gh-account", reference_id
        ),
        actor_class=actor_class,
        expected_actor_id=f"actor-{profile_id}",
        capability_ids=_CAPABILITIES,
        revision=_SHA,
    )
    api_identity: StoredGhAccountSpec | GitHubAppInstallationSpec
    if is_agent:
        api_identity = GitHubAppInstallationSpec(
            reference_id=reference_id,
            profile_id=profile_id,
            host="github.com",
            app_id="app-42",
            installation_id="installation-84",
            actor_id=f"actor-{profile_id}",
            repository_id=repository_id,
            capability_ids=_CAPABILITIES,
            permissions=(("contents", "write"),),
        )
    else:
        api_identity = StoredGhAccountSpec(
            reference_id=reference_id,
            profile_id=profile_id,
            host="github.com",
            login=f"{profile_id}-login",
            actor_id=f"actor-{profile_id}",
            actor_class=actor_class,
            repository_id=repository_id,
            capability_ids=_CAPABILITIES,
        )
    return AuthProfileConfig(
        profile=profile,
        eligibility=CredentialProfileEligibility(
            profile=profile,
            enabled=True,
            repository_patterns=(f"github.com/acme/{repository_name}",),
            boundary_id=f"boundary-{profile_id}",
        ),
        api_identity=api_identity,
        transport=GitTransportSpec(
            profile_id=profile_id,
            repository_id=repository_id,
            target_id=repository_id,
            provider_host="github.com",
            kind=GitTransportKind.HTTPS,
            credential_fingerprint=_SHA,
            allowed_access=(GitTransportAccess.READ, GitTransportAccess.WRITE),
            https_token_environment="REPOFORGE_GIT_HTTPS_TOKEN",
        ),
    )


def _observation(repo_id: str) -> RepositoryIdentityObservation:
    repository_id, name = {
        "alpha": ("111111", "alpha"),
        "beta": ("222222", "beta"),
    }[repo_id]
    return RepositoryIdentityObservation(
        provider=RepositoryProvider.GITHUB,
        provider_host="github.com",
        repository_id=repository_id,
        canonical_name=f"github.com/acme/{name}",
        exists=True,
        observed_at=NOW,
        config_revision=_SHA,
    )


def _binding(
    repo_id: str,
    *,
    human: str | None = None,
    agent: str | None = None,
) -> RepositoryIdentityBinding:
    observation = _observation(repo_id)
    return RepositoryIdentityBinding(
        provider=observation.provider,
        provider_host=observation.provider_host,
        repository_id=observation.repository_id,
        canonical_name=observation.canonical_name,
        human_profile_id=human,
        agent_profile_id=agent,
        config_revision=_SHA,
    )


def _material(
    profile_id: str,
    repository_id: str,
    *,
    actor_class: ActorClass,
) -> AuthMaterial:
    return AuthMaterial(
        material_id=f"material-{profile_id}",
        profile_id=profile_id,
        actor_class=actor_class,
        target_kind=AuthTargetKind.REPOSITORY,
        target_id=repository_id,
        capability_ids=_CAPABILITIES,
        issued_at="2026-07-30T23:55:00+00:00",
        expires_at="2026-07-31T00:30:00+00:00",
        state=AuthMaterialState.ACTIVE,
        actor_id=f"actor-{profile_id}",
        environment=(
            AuthEnvironmentBinding(
                "GH_TOKEN", EphemeralSecret.from_text(f"token-{profile_id}-canary-123456")
            ),
        ),
    )


def _runtime(
    tmp_path: Path,
    *,
    bindings: tuple[RepositoryIdentityBinding, ...],
    provider: object | None = None,
) -> tuple[RepositoryIdentityRuntime, DeterministicAuthMaterialProvider | object]:
    profiles = {
        "personal": _profile_config("personal", repository_id="111111", repository_name="alpha"),
        "automation": _profile_config(
            "automation",
            repository_id="222222",
            repository_name="beta",
            actor_class=ActorClass.AUTONOMOUS_AGENT,
        ),
    }
    config = AppConfig(
        source_path=tmp_path / "config.toml",
        server=ServerConfig(tmp_path / "workspaces", tmp_path / "state"),
        repositories={
            "alpha": RepositoryConfig("alpha", tmp_path / "alpha"),
            "beta": RepositoryConfig("beta", tmp_path / "beta"),
        },
        auth_profiles=profiles,
    )
    store = JsonRepositoryBindingStore(tmp_path / "state", FcntlLockManager(tmp_path / "locks"))
    for binding in bindings:
        store.create(binding)
    selected_provider = provider or DeterministicAuthMaterialProvider(
        {
            "personal-credential-v1": _material(
                "personal", "111111", actor_class=ActorClass.HUMAN_OPERATED
            ),
            "automation-credential-v1": _material(
                "automation", "222222", actor_class=ActorClass.AUTONOMOUS_AGENT
            ),
        }
    )
    return (
        RepositoryIdentityRuntime(
            config=config,
            bindings=store,
            broker=RepositoryAuthBroker(selected_provider),  # type: ignore[arg-type]
            observe=lambda repo_id, selector: _observation(repo_id),
            clock=FixedClock(NOW),
        ),
        selected_provider,
    )


def test_unique_auto_selection_requires_and_returns_the_durable_binding(tmp_path: Path) -> None:
    runtime, _provider = _runtime(
        tmp_path,
        bindings=(_binding("alpha", human="personal"),),
    )

    admission = runtime.resolve(repo_id="alpha", selector=AuthProfileSelector())

    assert admission.repo_id == "alpha"
    assert admission.profile.profile_id == "personal"
    assert admission.binding.human_profile_id == "personal"
    assert admission.binding_revision == Revision(1)
    assert admission.observation.repository_id == "111111"


def test_explicit_profile_cannot_override_an_exact_binding(tmp_path: Path) -> None:
    runtime, _provider = _runtime(
        tmp_path,
        bindings=(_binding("alpha", human="personal"),),
    )

    with pytest.raises(RepoForgeError) as failure:
        runtime.resolve(
            repo_id="alpha",
            selector=AuthProfileSelector("automation", RequestedActorClass.HUMAN),
        )

    assert failure.value.code is ErrorCode.CREDENTIAL_SCOPE_MISMATCH


def test_effectful_admission_never_persists_a_binding_proposal(tmp_path: Path) -> None:
    runtime, _provider = _runtime(tmp_path, bindings=())

    with pytest.raises(RepoForgeError) as failure:
        runtime.resolve(repo_id="alpha", selector=AuthProfileSelector())

    assert failure.value.code is ErrorCode.OPERATION_IDENTITY_NOT_FOUND


def test_actor_role_mismatch_fails_before_broker_admission(tmp_path: Path) -> None:
    runtime, provider = _runtime(
        tmp_path,
        bindings=(_binding("beta", agent="automation"),),
    )

    with pytest.raises(RepoForgeError) as failure:
        runtime.resolve(
            repo_id="beta",
            selector=AuthProfileSelector("automation", RequestedActorClass.HUMAN),
        )

    assert failure.value.code is ErrorCode.CREDENTIAL_SCOPE_MISMATCH
    assert isinstance(provider, DeterministicAuthMaterialProvider)
    assert provider.resolve_calls == []


def test_session_enforces_the_profile_capability_ceiling(tmp_path: Path) -> None:
    runtime, _provider = _runtime(
        tmp_path,
        bindings=(_binding("alpha", human="personal"),),
    )
    admission = runtime.resolve(repo_id="alpha", selector=AuthProfileSelector())

    with pytest.raises(RepoForgeError) as failure:
        runtime.session(
            admission,
            target_kind=AuthTargetKind.REPOSITORY,
            target_id="111111",
            required_capability_ids=("github.issues.write",),
        )

    assert failure.value.code is ErrorCode.CREDENTIAL_CAPABILITY_DENIED


def test_typed_provider_failure_propagates_through_runtime_session(tmp_path: Path) -> None:
    typed = RepoForgeError(
        "organization authorization required",
        code=ErrorCode.GITHUB_SSO_AUTHORIZATION_REQUIRED,
        retryable=False,
    )

    class Provider:
        def resolve(self, reference: OpaqueCredentialReference) -> AuthMaterial | None:
            del reference
            raise typed

        def refresh(
            self, reference: OpaqueCredentialReference, previous: AuthMaterial
        ) -> AuthMaterial | None:
            del reference, previous
            return None

        def release(self, material: AuthMaterial) -> None:
            material.release()

    runtime, _provider = _runtime(
        tmp_path,
        bindings=(_binding("alpha", human="personal"),),
        provider=Provider(),
    )
    admission = runtime.resolve(repo_id="alpha", selector=AuthProfileSelector())

    with pytest.raises(RepoForgeError) as failure:
        runtime.session(
            admission,
            target_kind=AuthTargetKind.REPOSITORY,
            target_id="111111",
            required_capability_ids=("github.contents.read",),
        )

    assert failure.value is typed


def test_two_profiles_hold_independent_sessions_without_global_switching(tmp_path: Path) -> None:
    runtime, provider = _runtime(
        tmp_path,
        bindings=(
            _binding("alpha", human="personal"),
            _binding("beta", agent="automation"),
        ),
    )
    personal = runtime.resolve(repo_id="alpha", selector=AuthProfileSelector())
    automation = runtime.resolve(
        repo_id="beta",
        selector=AuthProfileSelector("automation", RequestedActorClass.AGENT),
    )

    with (
        runtime.session(
            personal,
            target_kind=AuthTargetKind.REPOSITORY,
            target_id="111111",
            required_capability_ids=("github.contents.read",),
        ) as personal_session,
        runtime.session(
            automation,
            target_kind=AuthTargetKind.REPOSITORY,
            target_id="222222",
            required_capability_ids=("github.contents.write",),
        ) as automation_session,
    ):
        personal_context = personal_session.process_context(
            {"GH_TOKEN": "wrong-active-token", "SSH_AUTH_SOCK": "/tmp/wrong-agent"}
        )
        automation_context = automation_session.process_context(
            {"GH_TOKEN": "wrong-active-token", "SSH_AUTH_SOCK": "/tmp/wrong-agent"}
        )

        assert personal_context.profile_id == "personal"
        assert automation_context.profile_id == "automation"
        assert personal_context.environment_dict()["GH_TOKEN"].startswith("token-personal")
        assert automation_context.environment_dict()["GH_TOKEN"].startswith("token-automation")
        assert "SSH_AUTH_SOCK" not in personal_context.environment_dict()
        assert "SSH_AUTH_SOCK" not in automation_context.environment_dict()

    assert isinstance(provider, DeterministicAuthMaterialProvider)
    assert provider.release_calls == ["material-automation", "material-personal"]
