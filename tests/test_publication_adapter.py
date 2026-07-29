from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from repoforge.adapters.publication import PublicationAdapter
from repoforge.domain.errors import ErrorCode, RepoForgeError
from repoforge.domain.git_transport_identity import (
    GitTransportAccess,
    GitTransportActorEvidence,
    GitTransportEvidence,
    GitTransportKind,
    GitTransportSpec,
)
from repoforge.domain.repository_auth_broker import ProcessAuthContext
from repoforge.domain.repository_identity import (
    ActorClass,
    AuthLease,
    AuthLeaseState,
    AuthTargetKind,
    IdentityEvidenceKind,
    IdentitySurface,
    IdentitySurfaceEvidence,
    OpaqueCredentialReference,
    PublicationIntent,
    PublicationKind,
    RepositoryProvider,
)
from repoforge.ports.command import CommandResult
from repoforge.ports.publication import (
    PublicationAuthorization,
    PublicationEffect,
    PublicationRepositoryMetadata,
    PullRequestPublication,
)

_ROOT = Path("/workspace")
_NOW = "2026-07-29T07:00:00+00:00"
_COMMIT = "1" * 40
_TREE = "2" * 40
_CAPABILITY = "3" * 64
_PERMISSION = "4" * 64
_REMOTE_VERSION = "5" * 64
_CREDENTIAL = "6" * 64
_API_EVIDENCE = "7" * 64
_PUSH_EVIDENCE = "8" * 64


class FakeCommands:
    def __init__(self, outputs: dict[tuple[str, ...], str]) -> None:
        self.outputs = outputs
        self.calls: list[tuple[str, ...]] = []

    def run(self, argv: list[str], *, cwd: Path, **_kwargs: object) -> CommandResult:
        key = tuple(argv)
        self.calls.append(key)
        return CommandResult(key, str(cwd), 0, self.outputs.get(key, ""), "")


class FakeResolver:
    def __init__(
        self,
        by_url: dict[str, PublicationRepositoryMetadata],
        by_id: dict[str, PublicationRepositoryMetadata],
    ) -> None:
        self.by_url = by_url
        self.by_id = by_id
        self.url_calls: list[str] = []
        self.id_calls: list[str] = []

    def resolve_url(self, url: str) -> PublicationRepositoryMetadata:
        self.url_calls.append(url)
        return self.by_url[url]

    def resolve_id(self, repository_id: str) -> PublicationRepositoryMetadata:
        self.id_calls.append(repository_id)
        return self.by_id[repository_id]


class FakeAuthorizationGateway:
    def __init__(self, live: PublicationAuthorization) -> None:
        self.live = live
        self.calls: list[str] = []

    def revalidate(
        self,
        intent: PublicationIntent,
        expected: PublicationAuthorization,
    ) -> PublicationAuthorization:
        self.calls.append(intent.publication_id)
        assert expected.profile_id == self.live.profile_id
        return self.live


class FakeTransport:
    def __init__(self, *, observed_sha: str | None = _COMMIT) -> None:
        self.observed_sha = observed_sha
        self.push_calls: list[tuple[str, str]] = []
        self.ls_remote_calls: list[tuple[str, str | None]] = []

    def push(
        self,
        cwd: Path,
        remote_url: str,
        refspec: str,
        spec: GitTransportSpec,
        context: ProcessAuthContext,
    ) -> CommandResult:
        assert cwd == _ROOT
        assert spec.repository_id == context.target_id
        self.push_calls.append((remote_url, refspec))
        return CommandResult(("git", "push", remote_url, refspec), str(cwd), 0, "", "")

    def ls_remote(
        self,
        cwd: Path,
        remote_url: str,
        requested_ref: str | None,
        spec: GitTransportSpec,
        context: ProcessAuthContext,
    ) -> GitTransportEvidence:
        assert cwd == _ROOT
        assert spec.repository_id == context.target_id
        self.ls_remote_calls.append((remote_url, requested_ref))
        return GitTransportEvidence(
            profile_id=spec.profile_id,
            repository_id=spec.repository_id,
            provider_host=spec.provider_host,
            kind=spec.kind,
            credential_fingerprint=spec.credential_fingerprint,
            access=GitTransportAccess.READ,
            remote_url_digest="9" * 64,
            requested_ref=requested_ref,
            observed_sha=self.observed_sha,
            actor_evidence=GitTransportActorEvidence.UNOBSERVABLE,
        )


class FakeGitHubPublication:
    def __init__(self) -> None:
        self.create_calls: list[dict[str, object]] = []
        self.find_calls: list[dict[str, object]] = []
        self.found: PublicationEffect | None = None

    def create_pull_request(self, **kwargs: object) -> PublicationEffect:
        self.create_calls.append(dict(kwargs))
        return PublicationEffect(
            publication_id=str(kwargs["publication_id"]),
            kind=PublicationKind.PULL_REQUEST,
            destination_repository_id=str(kwargs["base_repository_id"]),
            destination_ref=str(kwargs["base_ref"]),
            commit_sha=str(kwargs["expected_commit_sha"]),
            external_id="pr-42",
            url="https://github.com/company/project/pull/42",
            reconciled=False,
        )

    def find_pull_request(self, **kwargs: object) -> PublicationEffect | None:
        self.find_calls.append(dict(kwargs))
        return self.found


def _metadata(
    repository_id: str,
    canonical_name: str,
    boundary_id: str,
) -> PublicationRepositoryMetadata:
    return PublicationRepositoryMetadata(repository_id, canonical_name, boundary_id)


_UPSTREAM = _metadata("repo-upstream", "github.com/upstream/project", "company")
_COMPANY = _metadata("repo-company", "github.com/company/project", "company")
_PERSONAL = _metadata("repo-personal", "github.com/personal/project", "personal")


def _commands(
    *,
    fetch_url: str = "git@github.com:upstream/project.git",
    push_urls: tuple[str, ...] = ("work:project.git",),
    rewrites: tuple[str, ...] = ("url.git@github.com:company/.pushInsteadOf work:",),
    commit: str = _COMMIT,
    tree: str = _TREE,
) -> FakeCommands:
    return FakeCommands(
        {
            ("git", "config", "--get-all", "remote.origin.url"): fetch_url + "\n",
            ("git", "config", "--get-all", "remote.origin.pushurl"): "\n".join(push_urls),
            ("git", "config", "--get-regexp", r"^url\..*\.(insteadOf|pushInsteadOf)$"): (
                "\n".join(rewrites)
            ),
            ("git", "rev-parse", "refs/heads/ai/publication^{commit}"): commit + "\n",
            ("git", "rev-parse", "refs/heads/ai/publication^{tree}"): tree + "\n",
        }
    )


def _resolver() -> FakeResolver:
    return FakeResolver(
        {
            "git@github.com:upstream/project.git": _UPSTREAM,
            "git@github.com:company/project.git": _COMPANY,
            "git@github.com:personal/project.git": _PERSONAL,
        },
        {item.repository_id: item for item in (_UPSTREAM, _COMPANY, _PERSONAL)},
    )


def _lease(repository_id: str = "repo-company") -> AuthLease:
    return AuthLease(
        lease_id="lease-publication",
        profile_id="company-app",
        provider=RepositoryProvider.GITHUB,
        repository_id=repository_id,
        target_kind=AuthTargetKind.REPOSITORY,
        target_id=f"github-repository-{repository_id}",
        actor_id="installation:84",
        credential_ref=OpaqueCredentialReference("github-app", "company-app"),
        issued_at="2026-07-29T06:00:00+00:00",
        expires_at="2026-07-29T08:00:00+00:00",
        state=AuthLeaseState.ACTIVE,
        config_revision="a" * 64,
        policy_revision="b" * 64,
        material_digest="c" * 64,
        provider_metadata=(("installation_id", "installation-84"),),
    )


def _surface(surface: IdentitySurface, repository_id: str) -> IdentitySurfaceEvidence:
    api = surface is IdentitySurface.GITHUB_API
    return IdentitySurfaceEvidence(
        surface=surface,
        evidence_kind=(
            IdentityEvidenceKind.VERIFIED_ACTOR
            if api
            else IdentityEvidenceKind.TRANSPORT_ACCESS_PROOF
        ),
        repository_id=repository_id,
        profile_id="company-app",
        actor_id="installation:84" if api else None,
        target="github.com/company/project",
        observed_at=_NOW,
        evidence_digest=_API_EVIDENCE if api else _PUSH_EVIDENCE,
    )


def _authorization(
    repository_id: str = "repo-company",
    approval_id: str | None = "approval-upstream-company",
) -> PublicationAuthorization:
    return PublicationAuthorization(
        profile_id="company-app",
        actor_class=ActorClass.AUTONOMOUS_AGENT,
        actor_id="installation:84",
        installation_id="installation-84",
        lease=_lease(repository_id),
        identity_surfaces=(
            _surface(IdentitySurface.GITHUB_API, repository_id),
            _surface(IdentitySurface.GIT_PUSH, repository_id),
        ),
        capability_digest=_CAPABILITY,
        permission_digest=_PERMISSION,
        remote_version=_REMOTE_VERSION,
        observed_at=_NOW,
        approved_cross_boundary_id=approval_id,
    )


def _push_intent(
    *,
    destination_repository_id: str = "repo-company",
    approval_id: str | None = "approval-upstream-company",
) -> PublicationIntent:
    return PublicationIntent(
        publication_id="publication-push",
        operation_id="op-" + "d" * 24,
        kind=PublicationKind.GIT_PUSH,
        source_repository_id="repo-upstream",
        destination_repository_id=destination_repository_id,
        remote_name="origin",
        source_ref="refs/heads/ai/publication",
        destination_ref="refs/heads/ai/publication",
        expected_commit_sha=_COMMIT,
        expected_tree_sha=_TREE,
        cross_boundary_approval_id=approval_id,
    )


def _transport_identity(
    repository_id: str = "repo-company",
) -> tuple[GitTransportSpec, ProcessAuthContext]:
    spec = GitTransportSpec(
        profile_id="company-app",
        repository_id=repository_id,
        target_id=repository_id,
        provider_host="github.com",
        kind=GitTransportKind.SSH,
        credential_fingerprint=_CREDENTIAL,
        allowed_access=(GitTransportAccess.READ, GitTransportAccess.WRITE),
        ssh_identity_file="/identity/company-app",
    )
    context = ProcessAuthContext(
        profile_id="company-app",
        material_id="material-company-app",
        target_kind=AuthTargetKind.REPOSITORY,
        target_id=repository_id,
        environment=(),
    )
    return spec, context


def _adapter(
    *,
    commands: FakeCommands | None = None,
    resolver: FakeResolver | None = None,
    live: PublicationAuthorization | None = None,
    transport: FakeTransport | None = None,
    github: FakeGitHubPublication | None = None,
) -> PublicationAdapter:
    return PublicationAdapter(
        commands=commands or _commands(),
        repositories=resolver or _resolver(),
        authorization=FakeAuthorizationGateway(live or _authorization()),
        transport=transport or FakeTransport(),
        github=github or FakeGitHubPublication(),
        clock=lambda: _NOW,
    )


def test_inspect_applies_pushinstead_of_and_binds_stable_repository_ids() -> None:
    resolver = _resolver()
    topology = _adapter(resolver=resolver).inspect(_ROOT, _push_intent())

    assert topology.fetch.repository_id == "repo-upstream"
    assert topology.push.repository_id == "repo-company"
    assert topology.push.canonical_name == "github.com/company/project"
    assert resolver.url_calls == [
        "git@github.com:upstream/project.git",
        "git@github.com:company/project.git",
    ]
    assert len(topology.push.url_digests) == 1
    assert len(topology.rewrite_digest) == 64


def test_inspect_rejects_multiple_effective_push_targets() -> None:
    commands = _commands(
        push_urls=("work:project.git", "personal:project.git"),
        rewrites=(
            "url.git@github.com:company/.pushInsteadOf work:",
            "url.git@github.com:personal/.pushInsteadOf personal:",
        ),
    )

    with pytest.raises(RepoForgeError) as failure:
        _adapter(commands=commands).inspect(_ROOT, _push_intent())

    assert failure.value.code is ErrorCode.PUBLICATION_TARGET_MISMATCH


def test_revalidate_detects_pushurl_drift_before_effect() -> None:
    adapter = _adapter()
    intent = _push_intent()
    preflight = adapter.inspect(_ROOT, intent)
    adapter._commands = _commands(  # type: ignore[attr-defined]
        push_urls=("personal:project.git",),
        rewrites=("url.git@github.com:personal/.pushInsteadOf personal:",),
    )

    with pytest.raises(RepoForgeError) as failure:
        adapter.revalidate(_ROOT, intent, preflight, _authorization())

    assert failure.value.code is ErrorCode.PUBLICATION_TARGET_MISMATCH


def test_publish_push_uses_only_the_reviewed_exact_refspec() -> None:
    transport = FakeTransport()
    adapter = _adapter(transport=transport)
    intent = _push_intent()
    topology = adapter.inspect(_ROOT, intent)
    reviewed = adapter.revalidate(_ROOT, intent, topology, _authorization())
    spec, context = _transport_identity()

    effect = adapter.publish(
        _ROOT,
        reviewed,
        topology,
        transport_spec=spec,
        auth_context=context,
    )

    assert transport.push_calls == [
        (
            "git@github.com:company/project.git",
            "refs/heads/ai/publication:refs/heads/ai/publication",
        )
    ]
    assert effect.commit_sha == _COMMIT
    assert effect.destination_repository_id == "repo-company"


def test_pull_request_publication_passes_explicit_base_head_repositories_and_refs() -> None:
    github = FakeGitHubPublication()
    adapter = _adapter(github=github)
    intent = PublicationIntent(
        publication_id="publication-pr",
        operation_id="op-" + "e" * 24,
        kind=PublicationKind.PULL_REQUEST,
        source_repository_id="repo-company",
        destination_repository_id="repo-upstream",
        remote_name="origin",
        source_ref="refs/heads/ai/publication",
        destination_ref="refs/heads/main",
        expected_commit_sha=_COMMIT,
        expected_tree_sha=_TREE,
        base_ref="refs/heads/main",
        head_ref="refs/heads/ai/publication",
        cross_boundary_approval_id="approval-company-fork",
    )
    authorization = replace(
        _authorization("repo-upstream"),
        identity_surfaces=(
            _surface(IdentitySurface.GITHUB_API, "repo-upstream"),
            _surface(IdentitySurface.PULL_REQUEST, "repo-upstream"),
        ),
        approved_cross_boundary_id="approval-company-fork",
    )
    adapter._authorization = FakeAuthorizationGateway(authorization)  # type: ignore[attr-defined]
    topology = adapter.inspect(_ROOT, intent)
    reviewed = adapter.revalidate(_ROOT, intent, topology, authorization)
    spec, context = _transport_identity("repo-upstream")

    effect = adapter.publish(
        _ROOT,
        reviewed,
        topology,
        transport_spec=spec,
        auth_context=context,
        pull_request=PullRequestPublication("Guard publication", "Exact intent"),
    )

    assert effect.external_id == "pr-42"
    assert github.create_calls == [
        {
            "cwd": _ROOT,
            "publication_id": "publication-pr",
            "base_repository_id": "repo-upstream",
            "head_repository_id": "repo-company",
            "base_repository": "github.com/upstream/project",
            "head_repository": "github.com/company/project",
            "base_ref": "refs/heads/main",
            "head_ref": "refs/heads/ai/publication",
            "expected_commit_sha": _COMMIT,
            "title": "Guard publication",
            "body": "Exact intent",
            "auth_context": context,
        }
    ]


def test_reconcile_push_queries_only_the_exact_destination_ref() -> None:
    transport = FakeTransport(observed_sha=_COMMIT)
    adapter = _adapter(transport=transport)
    intent = _push_intent()
    topology = adapter.inspect(_ROOT, intent)
    reviewed = adapter.revalidate(_ROOT, intent, topology, _authorization())
    spec, context = _transport_identity()

    effect = adapter.reconcile(
        _ROOT,
        reviewed,
        topology,
        transport_spec=spec,
        auth_context=context,
    )

    assert effect is not None and effect.reconciled is True
    assert transport.ls_remote_calls == [
        ("git@github.com:company/project.git", "refs/heads/ai/publication")
    ]


def test_reconcile_does_not_accept_a_different_commit() -> None:
    transport = FakeTransport(observed_sha="f" * 40)
    adapter = _adapter(transport=transport)
    intent = _push_intent()
    topology = adapter.inspect(_ROOT, intent)
    reviewed = adapter.revalidate(_ROOT, intent, topology, _authorization())
    spec, context = _transport_identity()

    assert (
        adapter.reconcile(
            _ROOT,
            reviewed,
            topology,
            transport_spec=spec,
            auth_context=context,
        )
        is None
    )
