from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from repoforge.adapters.github.gh_cli import GhCliGateway
from repoforge.adapters.publication import PublicationAdapter
from repoforge.config import ServerConfig
from repoforge.domain.errors import ErrorCode, RepoForgeError
from repoforge.domain.git_transport_identity import (
    GitTransportAccess,
    GitTransportActorEvidence,
    GitTransportEvidence,
    GitTransportKind,
    GitTransportSpec,
)
from repoforge.domain.github_capability_preflight import (
    GitHubCapabilityEvidenceState,
    GitHubCapabilityPreflightReport,
    GitHubCapabilityPreflightRequest,
    GitHubCapabilityResult,
    GitHubOperationCapability,
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
_PREFLIGHT_DETAIL = "9" * 64


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
    def __init__(
        self,
        live: PublicationAuthorization,
        events: list[str] | None = None,
    ) -> None:
        self.live = live
        self.calls: list[str] = []
        self.events = events

    def revalidate(
        self,
        intent: PublicationIntent,
        expected: PublicationAuthorization,
        *,
        requested_capability_ids: tuple[str, ...],
        auth_context: ProcessAuthContext,
    ) -> PublicationAuthorization:
        if self.events is not None:
            self.events.append("revalidate")
        self.calls.append(intent.publication_id)
        assert expected.profile_id == self.live.profile_id
        if requested_capability_ids:
            assert all(item.startswith("github.") for item in requested_capability_ids)
        assert auth_context.profile_id == expected.profile_id
        return self.live


class FakeCapabilityPreflight:
    def __init__(
        self,
        report: GitHubCapabilityPreflightReport,
        events: list[str] | None = None,
    ) -> None:
        self.report = report
        self.events = events
        self.calls: list[tuple[Path, GitHubCapabilityPreflightRequest, ProcessAuthContext]] = []

    def preflight(
        self,
        cwd: Path,
        request: GitHubCapabilityPreflightRequest,
        auth_context: ProcessAuthContext,
    ) -> GitHubCapabilityPreflightReport:
        if self.events is not None:
            self.events.append("capability_preflight")
        self.calls.append((cwd, request, auth_context))
        return self.report


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


class IsolatedGitHubCommands:
    def __init__(self, results: list[CommandResult]) -> None:
        self.results = results
        self.calls: list[dict[str, object]] = []

    def run(self, *_args: object, **_kwargs: object) -> CommandResult:
        raise AssertionError("publication must not use ambient gh execution")

    def run_isolated(self, argv: list[str], **kwargs: object) -> CommandResult:
        self.calls.append({"argv": tuple(argv), **kwargs})
        if not self.results:
            raise AssertionError(f"unhandled isolated command: {argv}")
        return self.results.pop(0)


def _github_auth_context(repository_id: str = "111") -> ProcessAuthContext:
    canary = "publication-credential-canary"
    return ProcessAuthContext(
        profile_id="company-app",
        material_id="material-company-app",
        target_kind=AuthTargetKind.REPOSITORY,
        target_id=repository_id,
        environment=(("GH_TOKEN", canary), ("GH_HOST", "github.com")),
        _secret_values=(canary,),
    )


def _pull_request_payload(
    *,
    publication_id: str = "publication-pr",
    head_sha: str = _COMMIT,
) -> dict[str, object]:
    return {
        "id": 9001,
        "number": 42,
        "html_url": "https://github.com/company/project/pull/42",
        "body": f"Exact intent\n\n<!-- repoforge-publication:{publication_id} -->",
        "head": {
            "sha": head_sha,
            "ref": "ai/publication",
            "repo": {"id": 222, "full_name": "personal/project"},
        },
        "base": {
            "ref": "main",
            "repo": {"id": 111, "full_name": "company/project"},
        },
    }


def _gh_publication_gateway(result_payload: object) -> tuple[GhCliGateway, IsolatedGitHubCommands]:
    commands = IsolatedGitHubCommands(
        [CommandResult(("gh", "api"), str(_ROOT), 0, json.dumps(result_payload), "")]
    )
    return (
        GhCliGateway(commands, ServerConfig(Path("/workspaces"), Path("/state"))),
        commands,
    )


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
        provider_metadata=(
            ("installation_id", "installation-84"),
            ("github_host", "github.com"),
            ("github_preflight_evidence_digest", _API_EVIDENCE),
            ("github_capability_digest", _CAPABILITY),
            ("github_permission_digest", _PERMISSION),
            ("github_preflight_observed_at", _NOW),
            ("config_revision", "a" * 64),
            ("policy_revision", "b" * 64),
        ),
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


def _preflight_report(
    *,
    state: GitHubCapabilityEvidenceState = GitHubCapabilityEvidenceState.PROVEN_AVAILABLE,
    error_code: ErrorCode | None = None,
    reason_code: str = "bounded_probe_succeeded",
    actor_id: str = "installation:84",
    repository_id: str = "repo-company",
    installation_id: str | None = "installation-84",
    capability: GitHubOperationCapability = GitHubOperationCapability.CONTENTS_WRITE,
    permission_ids: tuple[str, ...] = ("contents:write",),
) -> GitHubCapabilityPreflightReport:
    request = GitHubCapabilityPreflightRequest(
        host="github.com",
        actor_id=actor_id,
        repository_id=repository_id,
        installation_id=installation_id,
        capability_ids=(capability,),
        permission_ids=permission_ids,
        config_revision="a" * 64,
        policy_revision="b" * 64,
        observed_at=_NOW,
    )
    result = GitHubCapabilityResult(
        capability=capability,
        state=state,
        reason_code=reason_code,
        detail_digest=_PREFLIGHT_DETAIL,
        error_code=error_code,
        policy_category=None if error_code is None else "publication",
    )
    return GitHubCapabilityPreflightReport.build(request, (result,))


def _authorization_for_report(
    report: GitHubCapabilityPreflightReport,
) -> PublicationAuthorization:
    authorization = _authorization(report.repository_id)
    metadata = (
        ("installation_id", report.installation_id or ""),
        ("github_host", report.host),
        ("github_preflight_evidence_digest", "f" * 64),
        ("github_capability_digest", report.capability_digest),
        ("github_permission_digest", report.permission_digest),
        ("github_preflight_observed_at", "2026-07-29T06:00:00+00:00"),
        ("config_revision", report.config_revision),
        ("policy_revision", report.policy_revision),
    )
    return replace(
        authorization,
        lease=replace(authorization.lease, provider_metadata=metadata),
        capability_digest=report.capability_digest,
        permission_digest=report.permission_digest,
    )


def _legacy_preflight(
    repository_id: str = "repo-company",
    *,
    capability: GitHubOperationCapability = GitHubOperationCapability.CONTENTS_WRITE,
    permission_ids: tuple[str, ...] = ("contents:write",),
) -> GitHubCapabilityPreflightReport:
    return replace(
        _preflight_report(
            repository_id=repository_id,
            capability=capability,
            permission_ids=permission_ids,
        ),
        capability_digest=_CAPABILITY,
        permission_digest=_PERMISSION,
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
    capability_preflight: FakeCapabilityPreflight | None = None,
    transport: FakeTransport | None = None,
    github: FakeGitHubPublication | None = None,
) -> PublicationAdapter:
    return PublicationAdapter(
        commands=commands or _commands(),
        repositories=resolver or _resolver(),
        authorization=FakeAuthorizationGateway(live or _authorization()),
        capability_preflight=capability_preflight or FakeCapabilityPreflight(_legacy_preflight()),
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
        push_urls=("git@github.com:personal/project.git",),
    )
    _spec, context = _transport_identity()

    with pytest.raises(RepoForgeError) as failure:
        adapter.revalidate(
            _ROOT,
            intent,
            preflight,
            _authorization(),
            requested_capability_ids=(GitHubOperationCapability.CONTENTS_WRITE.value,),
            auth_context=context,
        )

    assert failure.value.code is ErrorCode.PUBLICATION_TARGET_MISMATCH


def test_publish_push_uses_only_the_reviewed_exact_refspec() -> None:
    transport = FakeTransport()
    adapter = _adapter(transport=transport)
    intent = _push_intent()
    topology = adapter.inspect(_ROOT, intent)
    spec, context = _transport_identity()
    reviewed = adapter.revalidate(
        _ROOT,
        intent,
        topology,
        _authorization(),
        requested_capability_ids=(GitHubOperationCapability.CONTENTS_WRITE.value,),
        auth_context=context,
    )

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
    adapter = _adapter(
        github=github,
        capability_preflight=FakeCapabilityPreflight(
            _legacy_preflight(
                "repo-upstream",
                capability=GitHubOperationCapability.PULL_REQUESTS_WRITE,
                permission_ids=("pull_requests:write",),
            )
        ),
    )
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
    spec, context = _transport_identity("repo-upstream")
    reviewed = adapter.revalidate(
        _ROOT,
        intent,
        topology,
        authorization,
        requested_capability_ids=(GitHubOperationCapability.PULL_REQUESTS_WRITE.value,),
        auth_context=context,
    )

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
    spec, context = _transport_identity()
    reviewed = adapter.revalidate(
        _ROOT,
        intent,
        topology,
        _authorization(),
        requested_capability_ids=(GitHubOperationCapability.CONTENTS_WRITE.value,),
        auth_context=context,
    )

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
    spec, context = _transport_identity()
    reviewed = adapter.revalidate(
        _ROOT,
        intent,
        topology,
        _authorization(),
        requested_capability_ids=(GitHubOperationCapability.CONTENTS_WRITE.value,),
        auth_context=context,
    )

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


def test_gh_cli_create_pull_request_uses_explicit_repositories_and_isolated_auth() -> None:
    gateway, commands = _gh_publication_gateway(_pull_request_payload())
    auth_context = _github_auth_context()

    effect = gateway.create_pull_request(
        cwd=_ROOT,
        publication_id="publication-pr",
        base_repository_id="111",
        head_repository_id="222",
        base_repository="github.com/company/project",
        head_repository="github.com/personal/project",
        base_ref="refs/heads/main",
        head_ref="refs/heads/ai/publication",
        expected_commit_sha=_COMMIT,
        title="Guard publication",
        body="Exact intent",
        auth_context=auth_context,
    )

    assert effect.external_id == "pr-42"
    assert effect.destination_repository_id == "111"
    assert effect.commit_sha == _COMMIT
    assert commands.calls[0]["environment"] == auth_context.environment_dict()
    assert commands.calls[0]["secrets"] == auth_context.secret_values
    argv = commands.calls[0]["argv"]
    assert "repos/company/project/pulls" in argv
    assert "repo" not in argv
    request = json.loads(str(commands.calls[0]["input_text"]))
    assert request["base"] == "main"
    assert request["head"] == "personal:ai/publication"
    assert request["draft"] is True
    assert "<!-- repoforge-publication:publication-pr -->" in request["body"]


def test_gh_cli_find_pull_request_requires_exact_marker_refs_sha_and_repository_ids() -> None:
    gateway, commands = _gh_publication_gateway([_pull_request_payload()])
    auth_context = _github_auth_context()

    effect = gateway.find_pull_request(
        cwd=_ROOT,
        publication_id="publication-pr",
        base_repository_id="111",
        head_repository_id="222",
        base_repository="github.com/company/project",
        head_repository="github.com/personal/project",
        base_ref="refs/heads/main",
        head_ref="refs/heads/ai/publication",
        expected_commit_sha=_COMMIT,
        auth_context=auth_context,
    )

    assert effect is not None and effect.reconciled is True
    assert "repos/company/project/pulls?" in commands.calls[0]["argv"][-1]
    assert commands.calls[0]["environment"] == auth_context.environment_dict()


def test_gh_cli_pull_request_response_identity_mismatch_fails_closed() -> None:
    gateway, _commands = _gh_publication_gateway(_pull_request_payload(head_sha="f" * 40))

    with pytest.raises(RepoForgeError) as failure:
        gateway.create_pull_request(
            cwd=_ROOT,
            publication_id="publication-pr",
            base_repository_id="111",
            head_repository_id="222",
            base_repository="github.com/company/project",
            head_repository="github.com/personal/project",
            base_ref="refs/heads/main",
            head_ref="refs/heads/ai/publication",
            expected_commit_sha=_COMMIT,
            title="Guard publication",
            body="Exact intent",
            auth_context=_github_auth_context(),
        )

    assert failure.value.code is ErrorCode.PUBLICATION_TARGET_MISMATCH


def test_write_time_preflight_runs_before_authorization_review_and_binds_evidence() -> None:
    events: list[str] = []
    report = _preflight_report()
    authorization = _authorization_for_report(report)
    capability_preflight = FakeCapabilityPreflight(report, events)
    authorization_gateway = FakeAuthorizationGateway(authorization, events)
    adapter = PublicationAdapter(
        commands=_commands(),
        repositories=_resolver(),
        authorization=authorization_gateway,
        capability_preflight=capability_preflight,
        transport=FakeTransport(),
        github=FakeGitHubPublication(),
        clock=lambda: _NOW,
    )
    intent = _push_intent()
    topology = adapter.inspect(_ROOT, intent)
    _spec, auth_context = _transport_identity()

    reviewed = adapter.revalidate(
        _ROOT,
        intent,
        topology,
        authorization,
        requested_capability_ids=(GitHubOperationCapability.CONTENTS_WRITE.value,),
        auth_context=auth_context,
    )

    assert events == ["capability_preflight", "revalidate"]
    assert authorization_gateway.calls == [intent.publication_id]
    assert len(capability_preflight.calls) == 1
    cwd, request, observed_context = capability_preflight.calls[0]
    assert cwd == _ROOT
    assert observed_context is auth_context
    assert request.actor_id == authorization.actor_id
    assert request.repository_id == intent.destination_repository_id
    assert request.installation_id == authorization.installation_id
    assert request.capability_ids == (GitHubOperationCapability.CONTENTS_WRITE,)
    assert request.permission_ids == ("contents:write",)
    assert request.config_revision == authorization.lease.config_revision
    assert request.policy_revision == authorization.lease.policy_revision
    assert reviewed.capability_digest == report.capability_digest
    assert reviewed.permission_digest == report.permission_digest
    assert report.evidence_digest in reviewed.evidence_digests


@pytest.mark.parametrize(
    ("state", "code", "reason"),
    [
        (
            GitHubCapabilityEvidenceState.PROVEN_DENIED,
            ErrorCode.GITHUB_SSO_AUTHORIZATION_REQUIRED,
            "sso_authorization_required",
        ),
        (
            GitHubCapabilityEvidenceState.LIKELY_POLICY_DENIED,
            ErrorCode.GITHUB_RULESET_POLICY_DENIED,
            "ruleset_policy_denied",
        ),
        (
            GitHubCapabilityEvidenceState.PROVIDER_UNAVAILABLE,
            ErrorCode.GITHUB_PROVIDER_UNAVAILABLE,
            "provider_unavailable",
        ),
        (
            GitHubCapabilityEvidenceState.UNOBSERVABLE,
            ErrorCode.GITHUB_ENTERPRISE_EVIDENCE_UNOBSERVABLE,
            "enterprise_evidence_unobservable",
        ),
    ],
)
def test_write_time_preflight_denial_stops_before_authorization_review(
    state: GitHubCapabilityEvidenceState,
    code: ErrorCode,
    reason: str,
) -> None:
    events: list[str] = []
    initial = _preflight_report()
    authorization = _authorization_for_report(initial)
    denied = _preflight_report(state=state, error_code=code, reason_code=reason)
    authorization_gateway = FakeAuthorizationGateway(authorization, events)
    adapter = PublicationAdapter(
        commands=_commands(),
        repositories=_resolver(),
        authorization=authorization_gateway,
        capability_preflight=FakeCapabilityPreflight(denied, events),
        transport=FakeTransport(),
        github=FakeGitHubPublication(),
        clock=lambda: _NOW,
    )
    intent = _push_intent()
    topology = adapter.inspect(_ROOT, intent)
    _spec, auth_context = _transport_identity()

    with pytest.raises(RepoForgeError) as failure:
        adapter.revalidate(
            _ROOT,
            intent,
            topology,
            authorization,
            requested_capability_ids=(GitHubOperationCapability.CONTENTS_WRITE.value,),
            auth_context=auth_context,
        )

    assert failure.value.code is code
    assert events == ["capability_preflight"]
    assert authorization_gateway.calls == []


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("actor_id", "other-actor", ErrorCode.OPERATION_IDENTITY_MISMATCH),
        ("installation_id", "installation-99", ErrorCode.OPERATION_IDENTITY_MISMATCH),
        ("repository_id", "repo-other", ErrorCode.GITHUB_API_REPOSITORY_MISMATCH),
        ("config_revision", "c" * 64, ErrorCode.OPERATION_IDENTITY_MISMATCH),
        ("policy_revision", "d" * 64, ErrorCode.OPERATION_IDENTITY_MISMATCH),
        ("capability_digest", "e" * 64, ErrorCode.CREDENTIAL_CAPABILITY_DENIED),
        ("permission_digest", "f" * 64, ErrorCode.GITHUB_API_PERMISSION_DENIED),
    ],
)
def test_write_time_preflight_identity_and_digest_drift_fails_closed(
    field: str,
    value: str,
    code: ErrorCode,
) -> None:
    events: list[str] = []
    initial = _preflight_report()
    authorization = _authorization_for_report(initial)
    drifted = replace(initial, **{field: value})
    authorization_gateway = FakeAuthorizationGateway(authorization, events)
    adapter = PublicationAdapter(
        commands=_commands(),
        repositories=_resolver(),
        authorization=authorization_gateway,
        capability_preflight=FakeCapabilityPreflight(drifted, events),
        transport=FakeTransport(),
        github=FakeGitHubPublication(),
        clock=lambda: _NOW,
    )
    intent = _push_intent()
    topology = adapter.inspect(_ROOT, intent)
    _spec, auth_context = _transport_identity()

    with pytest.raises(RepoForgeError) as failure:
        adapter.revalidate(
            _ROOT,
            intent,
            topology,
            authorization,
            requested_capability_ids=(GitHubOperationCapability.CONTENTS_WRITE.value,),
            auth_context=auth_context,
        )

    assert failure.value.code is code
    assert events == ["capability_preflight"]
    assert authorization_gateway.calls == []
