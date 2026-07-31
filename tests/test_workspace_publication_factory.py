from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from repoforge.adapters.persistence import JsonRepositoryBindingStore
from repoforge.adapters.publication_identity import (
    DurableBindingPublicationRepositoryResolver,
)
from repoforge.application.publication import PublicationOutcome, PublicationRequest
from repoforge.application.workspace.publication_request_factory import (
    ScopedWorkspacePublicationRequestFactory,
)
from repoforge.domain.auth_profile import AuthProfileSelector, RequestedActorClass
from repoforge.domain.errors import ErrorCode, RepoForgeError
from repoforge.domain.git_transport_identity import (
    GitTransportAccess,
    GitTransportKind,
    GitTransportSpec,
)
from repoforge.domain.repository_auth_broker import (
    AuthBrokerRequest,
    AuthEnvironmentBinding,
    AuthMaterial,
    AuthMaterialState,
    EphemeralSecret,
    ProcessAuthContext,
    RepositoryAuthBroker,
)
from repoforge.domain.repository_identity import (
    ActorClass,
    AuthLease,
    AuthLeaseState,
    AuthTargetKind,
    CredentialKind,
    CredentialProfile,
    OpaqueCredentialReference,
    PublicationKind,
    RepositoryIdentityBinding,
    RepositoryProvider,
)
from repoforge.ports.workspace_publication import (
    WorkspaceDraftPrPublication,
    WorkspacePushPublication,
)
from repoforge.testing.fakes import (
    FixedClock,
    InMemoryLockManager,
    SequenceIdGenerator,
)

_NOW = "2026-07-31T03:30:00+00:00"
_CONFIG = "a" * 64
_POLICY = "b" * 64
_HEAD = "1" * 40
_TREE = "2" * 40
_REMOTE = "3" * 40
_CAPABILITY = "4" * 64
_PERMISSION = "5" * 64
_API_EVIDENCE = "6" * 64
_TOKEN = "scoped-publication-token-canary"


def _profile() -> CredentialProfile:
    return CredentialProfile(
        profile_id="company",
        provider=RepositoryProvider.GITHUB,
        credential_kind=CredentialKind.STORED_ACCOUNT,
        credential_ref=OpaqueCredentialReference("gh-account", "company"),
        actor_class=ActorClass.HUMAN_OPERATED,
        expected_actor_id="user-42",
        capability_ids=(
            "github.contents.write",
            "github.pull_requests.write",
        ),
        revision="7" * 64,
    )


def _transport() -> GitTransportSpec:
    return GitTransportSpec(
        profile_id="company",
        repository_id="123456",
        target_id="123456",
        provider_host="github.com",
        kind=GitTransportKind.HTTPS,
        credential_fingerprint="8" * 64,
        allowed_access=(GitTransportAccess.READ, GitTransportAccess.WRITE),
        https_token_environment="REPOFORGE_GIT_HTTPS_TOKEN",
    )


def _lease() -> AuthLease:
    return AuthLease(
        lease_id="lease-" + "9" * 24,
        profile_id="company",
        provider=RepositoryProvider.GITHUB,
        repository_id="123456",
        target_kind=AuthTargetKind.REPOSITORY,
        target_id="123456",
        actor_id="user-42",
        credential_ref=OpaqueCredentialReference("gh-account", "company"),
        issued_at="2026-07-31T03:00:00+00:00",
        expires_at="2026-07-31T04:00:00+00:00",
        state=AuthLeaseState.ACTIVE,
        config_revision=_CONFIG,
        policy_revision=_POLICY,
        material_digest="c" * 64,
        provider_metadata=(
            ("github_host", "github.com"),
            ("repository_id", "123456"),
            ("github_preflight_evidence_digest", _API_EVIDENCE),
            ("github_capability_digest", _CAPABILITY),
            ("github_permission_digest", _PERMISSION),
            ("github_preflight_observed_at", _NOW),
            ("config_revision", _CONFIG),
            ("policy_revision", _POLICY),
        ),
    )


class _Provider:
    def __init__(self, material: AuthMaterial) -> None:
        self.material = material
        self.released = False

    def resolve(self, reference: OpaqueCredentialReference) -> AuthMaterial | None:
        assert reference.reference_id == "company"
        return self.material

    def refresh(
        self,
        reference: OpaqueCredentialReference,
        previous: AuthMaterial,
    ) -> AuthMaterial | None:
        del reference, previous
        return None

    def release(self, material: AuthMaterial) -> None:
        self.released = True
        material.release()


def test_broker_session_builds_safe_stable_repository_lease_and_releases_material() -> None:
    secret = EphemeralSecret.from_text(_TOKEN)
    material = AuthMaterial(
        material_id="material-company",
        profile_id="company",
        actor_class=ActorClass.HUMAN_OPERATED,
        target_kind=AuthTargetKind.REPOSITORY,
        target_id="123456",
        capability_ids=("github.contents.write",),
        issued_at="2026-07-31T03:00:00+00:00",
        expires_at="2026-07-31T04:00:00+00:00",
        state=AuthMaterialState.ACTIVE,
        actor_id="user-42",
        material_digest="c" * 64,
        provider_metadata=(("github_host", "github.com"),),
        environment=(AuthEnvironmentBinding("GH_TOKEN", secret),),
    )
    provider = _Provider(material)
    request = AuthBrokerRequest(
        profile=_profile(),
        target_kind=AuthTargetKind.REPOSITORY,
        target_id="123456",
        required_capability_ids=("github.contents.write",),
        allowed_environment_keys=("GH_TOKEN",),
        now=_NOW,
    )

    with RepositoryAuthBroker(provider).session(request) as session:
        lease = session.auth_lease(
            lease_id="lease-" + "d" * 24,
            config_revision=_CONFIG,
            policy_revision=_POLICY,
        )
        assert lease.repository_id == "123456"
        assert lease.target_id == "123456"
        assert lease.credential_ref == _profile().credential_ref
        assert lease.material_digest == material.material_digest
        assert secret.released is False

    assert provider.released is True
    assert secret.released is True


@dataclass
class _ScopedSession:
    lease: AuthLease
    released: bool = False

    def __enter__(self) -> _ScopedSession:
        return self

    def __exit__(self, *_args: object) -> None:
        self.released = True

    def process_context(self, _base: object) -> ProcessAuthContext:
        assert self.released is False
        return ProcessAuthContext(
            profile_id=self.lease.profile_id,
            material_id="material-company",
            target_kind=self.lease.target_kind,
            target_id=self.lease.target_id,
            environment=(("GH_TOKEN", _TOKEN),),
            _secret_values=(_TOKEN,),
        )

    def auth_lease(self, **_kwargs: object) -> AuthLease:
        assert self.released is False
        return self.lease


class _Runtime:
    def __init__(self, session: _ScopedSession) -> None:
        self.scoped = session
        self.selectors: list[AuthProfileSelector] = []
        self.required: list[tuple[str, ...]] = []
        self.admission = SimpleNamespace(
            profile=_profile(),
            observation=SimpleNamespace(
                repository_id="123456",
                canonical_name="github.com/acme/widgets",
                provider_host="github.com",
            ),
        )

    def resolve(self, *, repo_id: str, selector: AuthProfileSelector) -> object:
        assert repo_id == "repoforge"
        self.selectors.append(selector)
        return self.admission

    def session(
        self,
        admission: object,
        *,
        target_kind: AuthTargetKind,
        target_id: str,
        required_capability_ids: tuple[str, ...],
    ) -> _ScopedSession:
        assert admission is self.admission
        assert target_kind is AuthTargetKind.REPOSITORY
        assert target_id == "123456"
        self.required.append(required_capability_ids)
        return self.scoped


class _Commands:
    def environment(self, extra: object = None) -> dict[str, str]:
        del extra
        return {"HOME": "/home/demo", "GH_TOKEN": "wrong-global-token"}


def _factory(runtime: _Runtime) -> ScopedWorkspacePublicationRequestFactory:
    configured = SimpleNamespace(profile=_profile(), transport=_transport())
    config = SimpleNamespace(auth_profiles={"company": configured})
    return ScopedWorkspacePublicationRequestFactory(
        config=config,
        runtime=runtime,
        commands=_Commands(),
        clock=FixedClock(_NOW),
        ids=SequenceIdGenerator(
            (
                "1" * 24,
                "2" * 24,
                "3" * 24,
                "4" * 24,
            )
        ),
        config_revision=_CONFIG,
        policy_revision=_POLICY,
    )


def _outcome(request: PublicationRequest, *, url: str | None = None) -> PublicationOutcome:
    return PublicationOutcome(
        publication_id=request.intent.publication_id,
        kind=request.intent.kind,
        operation_id=request.intent.operation_id,
        receipt_id="receipt-" + "e" * 24,
        result_reference=f"operation-result:{request.intent.operation_id}",
        source_repository_id=request.intent.source_repository_id,
        destination_repository_id=request.intent.destination_repository_id,
        source_ref=request.intent.source_ref,
        destination_ref=request.intent.destination_ref,
        commit_sha=request.intent.expected_commit_sha,
        tree_sha=request.intent.expected_tree_sha or _TREE,
        preflight_evidence_digest=_API_EVIDENCE,
        review_digest="f" * 64,
        external_id="effect-publication",
        url=url,
        reconciled=False,
    )


def test_scoped_push_factory_keeps_request_inside_selected_broker_session() -> None:
    session = _ScopedSession(_lease())
    runtime = _Runtime(session)
    selector = AuthProfileSelector(
        auth_profile="company",
        actor_class=RequestedActorClass.HUMAN,
    )
    workspace_request = WorkspacePushPublication(
        workspace_id="workspace-1",
        repo_id="repoforge",
        cwd=Path("/workspace"),
        remote="origin",
        source_ref="refs/heads/feature",
        destination_ref="refs/heads/feature",
        head_sha=_HEAD,
        tree_sha=_TREE,
        remote_head_before=_REMOTE,
        idempotency_key=None,
        selector=selector,
    )
    observed: list[PublicationRequest] = []

    def execute(request: PublicationRequest) -> PublicationOutcome:
        assert session.released is False
        observed.append(request)
        assert request.intent.source_repository_id == "123456"
        assert request.intent.destination_repository_id == "123456"
        assert request.authorization.lease.target_id == "123456"
        assert request.transport_spec.target_id == "123456"
        assert request.auth_context.environment_dict()["GH_TOKEN"] == _TOKEN
        assert "wrong-global-token" not in request.auth_context.secret_values
        assert request.capability_id == "git.push"
        assert request.capability_requests[0].capability_ids == (
            "git.push",
            "github.contents.write",
        )
        assert request.idempotency_key.startswith("publication-")
        return _outcome(request)

    outcome = _factory(runtime).execute_push(workspace_request, execute)

    assert outcome.kind is PublicationKind.GIT_PUSH
    assert runtime.selectors == [selector]
    assert runtime.required == [("github.contents.write",)]
    assert len(observed) == 1
    assert session.released is True


def test_scoped_draft_pr_factory_uses_exact_refs_and_pull_request_capability() -> None:
    session = _ScopedSession(_lease())
    runtime = _Runtime(session)
    selector = AuthProfileSelector(
        auth_profile="company",
        actor_class=RequestedActorClass.HUMAN,
    )
    workspace_request = WorkspaceDraftPrPublication(
        workspace_id="workspace-1",
        repo_id="repoforge",
        cwd=Path("/workspace"),
        remote="origin",
        base_ref="refs/heads/main",
        head_ref="refs/heads/feature",
        head_sha=_HEAD,
        tree_sha=_TREE,
        title="Scoped publication",
        body="Exact reviewed body",
        idempotency_key="pr-key",
        selector=selector,
    )

    def execute(request: PublicationRequest) -> PublicationOutcome:
        assert request.intent.kind is PublicationKind.PULL_REQUEST
        assert request.intent.base_ref == "refs/heads/main"
        assert request.intent.head_ref == "refs/heads/feature"
        assert request.capability_id == "github.pull_requests.write"
        assert request.capability_requests[0].capability_ids == ("github.pull_requests.write",)
        assert request.pull_request is not None
        assert request.pull_request.title == "Scoped publication"
        return _outcome(request, url="https://github.com/acme/widgets/pull/42")

    outcome = _factory(runtime).execute_draft_pr(workspace_request, execute)

    assert outcome.url == "https://github.com/acme/widgets/pull/42"
    assert runtime.required == [("github.pull_requests.write",)]
    assert session.released is True


def test_durable_publication_resolver_accepts_only_the_exact_bound_url(
    tmp_path: Path,
) -> None:
    bindings = JsonRepositoryBindingStore(tmp_path, InMemoryLockManager())
    binding = RepositoryIdentityBinding(
        provider=RepositoryProvider.GITHUB,
        provider_host="github.com",
        repository_id="123456",
        canonical_name="github.com/acme/widgets",
        human_profile_id="company",
        agent_profile_id=None,
        config_revision=_CONFIG,
    )
    bindings.create(binding)
    resolver = DurableBindingPublicationRepositoryResolver(bindings)

    resolved = resolver.resolve_url("git@github.com:acme/widgets.git")

    assert resolved.repository_id == "123456"
    assert resolved.canonical_name == "github.com/acme/widgets"
    assert resolver.resolve_id("123456") == resolved
    with pytest.raises(RepoForgeError) as mismatch:
        resolver.resolve_url("https://github.com/personal/widgets.git")
    assert mismatch.value.code is ErrorCode.PUBLICATION_TARGET_MISMATCH
