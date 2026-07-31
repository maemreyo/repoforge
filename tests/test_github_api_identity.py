from __future__ import annotations

import json
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import Any, NoReturn, cast

import pytest

from repoforge.adapters.github.api_identity import (
    GhCliGitHubApiIdentityVerifier,
    GhCliGitHubAppInstallationTokenIssuer,
    GhCliStoredAccountTokenSource,
    GitHubApiAuthProvider,
    UnavailableGitHubAppInstallationTokenIssuer,
    github_api_auth_lease,
)
from repoforge.adapters.subprocess import SubprocessCommandExecutor
from repoforge.config import ServerConfig
from repoforge.domain.errors import ErrorCode, RepoForgeError
from repoforge.domain.github_api_identity import (
    GitHubApiIdentityKind,
    GitHubApiIdentityProof,
    GitHubApiTokenGrant,
    GitHubAppInstallationSpec,
    StoredGhAccountSpec,
)
from repoforge.domain.github_capability_preflight import (
    GitHubCapabilityEvidenceState,
    GitHubCapabilityPreflightReport,
    GitHubCapabilityPreflightRequest,
    GitHubCapabilityResult,
    GitHubOperationCapability,
)
from repoforge.domain.repository_auth_broker import EphemeralSecret, ProcessAuthContext
from repoforge.domain.repository_identity import ActorClass, OpaqueCredentialReference
from repoforge.ports.command import CommandResult
from repoforge.testing.fakes import FixedClock

_CONFIG = "a" * 64
_POLICY = "b" * 64
_CAPABILITIES = (
    GitHubOperationCapability.CONTENTS_READ.value,
    GitHubOperationCapability.ISSUES_WRITE.value,
    GitHubOperationCapability.PULL_REQUESTS_WRITE.value,
)


class StoredSource:
    def __init__(
        self, grants: dict[str, GitHubApiTokenGrant], *, active_login: str = "wrong"
    ) -> None:
        self.grants = grants
        self.active_login = active_login
        self.calls: list[str] = []
        self.failure: Exception | None = None

    def issue(self, spec: StoredGhAccountSpec) -> GitHubApiTokenGrant:
        self.calls.append(spec.login)
        if self.failure is not None:
            raise self.failure
        return self.grants[spec.reference_id]


class AppIssuer:
    def __init__(self, grants: dict[str, GitHubApiTokenGrant]) -> None:
        self.grants = grants
        self.calls: list[str] = []
        self.revoked: list[str] = []
        self.failure: Exception | None = None

    def issue(self, spec: GitHubAppInstallationSpec) -> GitHubApiTokenGrant:
        self.calls.append(spec.installation_id)
        if self.failure is not None:
            raise self.failure
        return self.grants[spec.reference_id]

    def revoke(self, grant: GitHubApiTokenGrant) -> None:
        self.revoked.append(grant.grant_id)
        grant.token.release()


class Verifier:
    def __init__(self, events: list[str] | None = None) -> None:
        self.proofs: dict[str, GitHubApiIdentityProof] = {}
        self.failure: Exception | None = None
        self.events = events

    def verify_stored_account(
        self, spec: StoredGhAccountSpec, grant: GitHubApiTokenGrant
    ) -> GitHubApiIdentityProof:
        del grant
        if self.events is not None:
            self.events.append("verify_stored")
        if self.failure is not None:
            raise self.failure
        return self.proofs[spec.reference_id]

    def verify_app_installation(
        self, spec: GitHubAppInstallationSpec, grant: GitHubApiTokenGrant
    ) -> GitHubApiIdentityProof:
        del grant
        if self.events is not None:
            self.events.append("verify_app")
        if self.failure is not None:
            raise self.failure
        return self.proofs[spec.reference_id]


class PreflightGateway:
    def __init__(
        self,
        *,
        events: list[str] | None = None,
        detail_digests: list[str] | None = None,
        failure: tuple[GitHubCapabilityEvidenceState, ErrorCode, str] | None = None,
    ) -> None:
        self.events = events
        self.detail_digests = detail_digests or ["d" * 64]
        self.failure = failure
        self.calls: list[tuple[Path, GitHubCapabilityPreflightRequest, ProcessAuthContext]] = []

    def preflight(
        self,
        cwd: Path,
        request: GitHubCapabilityPreflightRequest,
        auth_context: ProcessAuthContext,
    ) -> GitHubCapabilityPreflightReport:
        if self.events is not None:
            self.events.append("preflight")
        self.calls.append((cwd, request, auth_context))
        detail = (
            self.detail_digests.pop(0) if len(self.detail_digests) > 1 else self.detail_digests[0]
        )
        results: list[GitHubCapabilityResult] = []
        for capability in request.capability_ids:
            if self.failure is None:
                results.append(
                    GitHubCapabilityResult(
                        capability=capability,
                        state=GitHubCapabilityEvidenceState.PROVEN_AVAILABLE,
                        reason_code="test_preflight_available",
                        detail_digest=detail,
                    )
                )
            else:
                state, code, category = self.failure
                results.append(
                    GitHubCapabilityResult(
                        capability=capability,
                        state=state,
                        reason_code=category,
                        detail_digest=detail,
                        error_code=code,
                        policy_category=category,
                    )
                )
        return GitHubCapabilityPreflightReport.build(request, tuple(results))


class IsolatedExecutor:
    def __init__(self, result: CommandResult | list[CommandResult]) -> None:
        self.results = list(result) if isinstance(result, list) else [result]
        self.calls: list[dict[str, Any]] = []

    def environment(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        return {
            "HOME": "/home/demo",
            "PATH": "/safe/bin",
            "GH_HOST": "wrong.example",
            "SSH_AUTH_SOCK": "/tmp/wrong-agent",
            **dict(extra or {}),
        }

    def run_isolated(self, argv: list[str], **kwargs: Any) -> CommandResult:
        self.calls.append({"mode": "isolated", "argv": tuple(argv), **kwargs})
        if not self.results:
            raise AssertionError(f"unhandled isolated command: {argv}")
        return self.results.pop(0)

    def run_secret_text(self, argv: list[str], **kwargs: Any) -> EphemeralSecret:
        self.calls.append({"mode": "secret_text", "argv": tuple(argv), **kwargs})
        if not self.results:
            raise AssertionError(f"unhandled secret-text command: {argv}")
        result = self.results.pop(0)
        return EphemeralSecret.from_text(result.stdout.strip())

    def run_secret_json(
        self, argv: list[str], *, field: str, **kwargs: Any
    ) -> tuple[dict[str, object], EphemeralSecret]:
        self.calls.append({"mode": "secret_json", "argv": tuple(argv), "field": field, **kwargs})
        if not self.results:
            raise AssertionError(f"unhandled secret-json command: {argv}")
        payload = json.loads(self.results.pop(0).stdout)
        value = payload.pop(field)
        assert isinstance(value, str)
        return payload, EphemeralSecret.from_text(value)


class JwtSigner:
    def __init__(self, value: str = "jwt-canary-123456") -> None:
        self.value = value
        self.calls: list[dict[str, str]] = []
        self.issued: list[EphemeralSecret] = []

    def issue(self, *, app_id: str, issued_at: str, expires_at: str) -> EphemeralSecret:
        self.calls.append({"app_id": app_id, "issued_at": issued_at, "expires_at": expires_at})
        value = EphemeralSecret.from_text(self.value)
        self.issued.append(value)
        return value


def _stored(
    reference_id: str, login: str, actor_id: str, repository_id: str
) -> StoredGhAccountSpec:
    return StoredGhAccountSpec(
        reference_id=reference_id,
        profile_id=reference_id,
        host="github.com",
        login=login,
        actor_id=actor_id,
        actor_class=ActorClass.HUMAN_OPERATED,
        repository_id=repository_id,
        capability_ids=_CAPABILITIES,
    )


def _app() -> GitHubAppInstallationSpec:
    return GitHubAppInstallationSpec(
        reference_id="company-app",
        profile_id="company-app",
        host="github.com",
        app_id="app-42",
        installation_id="installation-84",
        actor_id="installation:84",
        repository_id="123456",
        capability_ids=_CAPABILITIES,
        permissions=(("contents", "read"), ("issues", "write"), ("pull_requests", "write")),
    )


def _grant(
    *,
    grant_id: str,
    kind: GitHubApiIdentityKind,
    token: str,
    actor_id: str,
    repository_id: str,
    capabilities: tuple[str, ...] = _CAPABILITIES,
    permissions: tuple[str, ...] = ("contents:read", "issues:write", "pull_requests:write"),
    installation_id: str | None = None,
    sso_authorized: bool = True,
    approved: bool = True,
    revoked: bool = False,
    issued_at: str = "2026-07-28T00:00:00+00:00",
    expires_at: str = "2026-07-28T00:05:00+00:00",
) -> GitHubApiTokenGrant:
    return GitHubApiTokenGrant(
        grant_id=grant_id,
        kind=kind,
        token=EphemeralSecret.from_text(token),
        actor_id=actor_id,
        repository_id=repository_id,
        capability_ids=capabilities,
        permission_ids=permissions,
        issued_at=issued_at,
        expires_at=expires_at,
        installation_id=installation_id,
        sso_authorized=sso_authorized,
        approved=approved,
        revoked=revoked,
    )


def _provider(
    stored_specs: tuple[StoredGhAccountSpec, ...],
    stored: StoredSource,
    app: AppIssuer,
    verifier: Verifier,
    app_specs: tuple[GitHubAppInstallationSpec, ...] = (),
    preflight: PreflightGateway | None = None,
) -> GitHubApiAuthProvider:
    return GitHubApiAuthProvider(
        stored_accounts=stored_specs,
        app_installations=app_specs,
        stored_source=stored,
        app_issuer=app,
        verifier=verifier,
        capability_preflight=preflight or PreflightGateway(),
        cwd=Path("/repo"),
        config_revision=_CONFIG,
        policy_revision=_POLICY,
    )


def test_specs_reject_legacy_coarse_capability_ids() -> None:
    with pytest.raises(ValueError, match="exact GitHub operation"):
        replace(_app(), capability_ids=("github_api_write",))


def test_provider_preserves_typed_stored_source_failure() -> None:
    spec = _stored("personal", "personal-login", "user-1", "987654")
    typed = RepoForgeError(
        "organization authorization required",
        code=ErrorCode.GITHUB_SSO_AUTHORIZATION_REQUIRED,
        retryable=False,
    )
    source = StoredSource({})
    source.failure = typed
    provider = _provider((spec,), source, AppIssuer({}), Verifier())

    with pytest.raises(RepoForgeError) as failure:
        provider.resolve(OpaqueCredentialReference("gh-account", spec.reference_id))

    assert failure.value is typed


def test_provider_preserves_typed_verifier_failure_and_releases_grant() -> None:
    spec = _stored("personal", "personal-login", "user-1", "987654")
    grant = _grant(
        grant_id="grant-typed-verifier-failure",
        kind=GitHubApiIdentityKind.STORED_ACCOUNT,
        token="typed-verifier-test-secret-123456",
        actor_id=spec.actor_id,
        repository_id=spec.repository_id,
    )
    typed = RepoForgeError(
        "live actor changed",
        code=ErrorCode.GITHUB_API_ACTOR_MISMATCH,
        retryable=False,
    )
    verifier = Verifier()
    verifier.failure = typed
    provider = _provider((spec,), StoredSource({spec.reference_id: grant}), AppIssuer({}), verifier)

    with pytest.raises(RepoForgeError) as failure:
        provider.resolve(OpaqueCredentialReference("gh-account", spec.reference_id))

    assert failure.value is typed
    assert grant.token.released is True


def test_unavailable_app_signer_failure_remains_typed_at_use() -> None:
    spec = _app()
    provider = GitHubApiAuthProvider(
        stored_accounts=(),
        app_installations=(spec,),
        stored_source=StoredSource({}),
        app_issuer=UnavailableGitHubAppInstallationTokenIssuer(),
        verifier=Verifier(),
        capability_preflight=PreflightGateway(),
        cwd=Path("/repo"),
        config_revision=_CONFIG,
        policy_revision=_POLICY,
    )

    with pytest.raises(RepoForgeError) as failure:
        provider.resolve(OpaqueCredentialReference("github-app", spec.reference_id))

    assert failure.value.code is ErrorCode.CREDENTIAL_REFERENCE_NOT_FOUND


def test_provider_runs_preflight_after_identity_proof_and_binds_safe_metadata() -> None:
    events: list[str] = []
    spec = _app()
    token = "preflight-material-token-canary-293"
    grant = _grant(
        grant_id="grant-preflight",
        kind=GitHubApiIdentityKind.APP_INSTALLATION,
        token=token,
        actor_id=spec.actor_id,
        repository_id=spec.repository_id,
        installation_id=spec.installation_id,
    )
    issuer = AppIssuer({spec.reference_id: grant})
    verifier = Verifier(events)
    verifier.proofs[spec.reference_id] = GitHubApiIdentityProof(
        spec.actor_id,
        spec.repository_id,
        spec.capability_ids,
        grant.permission_ids,
        installation_id=spec.installation_id,
    )
    preflight = PreflightGateway(events=events)
    provider = _provider(
        (),
        StoredSource({}),
        issuer,
        verifier,
        (spec,),
        preflight,
    )

    material = provider.resolve(OpaqueCredentialReference("github-app", spec.reference_id))

    assert material is not None
    assert events == ["verify_app", "preflight"]
    assert len(preflight.calls) == 1
    cwd, request, auth_context = preflight.calls[0]
    assert cwd == Path("/repo")
    assert request.actor_id == spec.actor_id
    assert request.repository_id == spec.repository_id
    assert request.installation_id == spec.installation_id
    assert request.capability_ids == tuple(
        GitHubOperationCapability(item) for item in spec.capability_ids
    )
    assert request.permission_ids == spec.permission_ids
    assert request.config_revision == _CONFIG
    assert request.policy_revision == _POLICY
    assert auth_context.target_id == spec.repository_id
    assert auth_context.secret_values == (token,)

    metadata = dict(material.provider_metadata)
    assert metadata == {
        "github_kind": "app_installation",
        "github_host": "github.com",
        "repository_id": "123456",
        "installation_id": "installation-84",
        "github_preflight_evidence_digest": metadata["github_preflight_evidence_digest"],
        "github_capability_digest": metadata["github_capability_digest"],
        "github_permission_digest": metadata["github_permission_digest"],
        "github_preflight_observed_at": grant.issued_at,
        "config_revision": _CONFIG,
        "policy_revision": _POLICY,
    }
    for key in (
        "github_preflight_evidence_digest",
        "github_capability_digest",
        "github_permission_digest",
    ):
        assert len(metadata[key]) == 64

    reference = OpaqueCredentialReference("github-app", spec.reference_id)
    lease = github_api_auth_lease(
        material,
        reference,
        lease_id="lease-preflight-company-app",
        config_revision=_CONFIG,
        policy_revision=_POLICY,
    )
    assert lease.provider_metadata == material.provider_metadata
    encoded = json.dumps(lease.payload(), sort_keys=True)
    assert token not in encoded
    assert token not in json.dumps(material.safe_payload(), sort_keys=True)


def test_auth_provider_does_not_fall_back_to_doctor_capability_probe() -> None:
    """Credential issuance requires the dedicated write-time preflight contract."""

    class LegacyDoctorProbe:
        def __init__(self) -> None:
            self.calls = 0

        def probe(self, *_args: object) -> NoReturn:
            self.calls += 1
            raise AssertionError("auth must not call the doctor capability probe")

    spec = _app()
    grant = _grant(
        grant_id="grant-no-doctor-fallback",
        kind=GitHubApiIdentityKind.APP_INSTALLATION,
        token="no-doctor-fallback-token",
        actor_id=spec.actor_id,
        repository_id=spec.repository_id,
        installation_id=spec.installation_id,
    )
    issuer = AppIssuer({spec.reference_id: grant})
    verifier = Verifier()
    verifier.proofs[spec.reference_id] = GitHubApiIdentityProof(
        spec.actor_id,
        spec.repository_id,
        spec.capability_ids,
        grant.permission_ids,
        installation_id=spec.installation_id,
    )
    legacy_probe = LegacyDoctorProbe()
    provider = _provider(
        (),
        StoredSource({}),
        issuer,
        verifier,
        (spec,),
        cast(PreflightGateway, legacy_probe),
    )

    with pytest.raises(RepoForgeError) as failure:
        provider.resolve(OpaqueCredentialReference("github-app", spec.reference_id))

    assert failure.value.code is ErrorCode.GITHUB_PROVIDER_UNAVAILABLE
    assert legacy_probe.calls == 0
    assert issuer.revoked == [grant.grant_id]


def test_preflight_denial_releases_grant_before_material_creation() -> None:
    spec = _app()
    grant = _grant(
        grant_id="grant-preflight-denied",
        kind=GitHubApiIdentityKind.APP_INSTALLATION,
        token="denied-preflight-token-canary-293",
        actor_id=spec.actor_id,
        repository_id=spec.repository_id,
        installation_id=spec.installation_id,
    )
    issuer = AppIssuer({spec.reference_id: grant})
    verifier = Verifier()
    verifier.proofs[spec.reference_id] = GitHubApiIdentityProof(
        spec.actor_id,
        spec.repository_id,
        spec.capability_ids,
        grant.permission_ids,
        installation_id=spec.installation_id,
    )
    preflight = PreflightGateway(
        failure=(
            GitHubCapabilityEvidenceState.LIKELY_POLICY_DENIED,
            ErrorCode.GITHUB_SSO_AUTHORIZATION_REQUIRED,
            "sso",
        )
    )
    provider = _provider((), StoredSource({}), issuer, verifier, (spec,), preflight)

    with pytest.raises(RepoForgeError) as failure:
        provider.resolve(OpaqueCredentialReference("github-app", spec.reference_id))

    assert failure.value.code is ErrorCode.GITHUB_SSO_AUTHORIZATION_REQUIRED
    assert issuer.revoked == [grant.grant_id]
    assert grant.token.released is True


def test_refresh_allows_new_observation_time_but_rejects_preflight_evidence_drift() -> None:
    spec = _app()
    initial = _grant(
        grant_id="grant-preflight-initial",
        kind=GitHubApiIdentityKind.APP_INSTALLATION,
        token="initial-preflight-token-canary-293",
        actor_id=spec.actor_id,
        repository_id=spec.repository_id,
        installation_id=spec.installation_id,
        issued_at="2026-07-28T00:00:00+00:00",
        expires_at="2026-07-28T00:30:00+00:00",
    )
    refreshed = _grant(
        grant_id="grant-preflight-refreshed",
        kind=GitHubApiIdentityKind.APP_INSTALLATION,
        token="refreshed-preflight-token-canary-293",
        actor_id=spec.actor_id,
        repository_id=spec.repository_id,
        installation_id=spec.installation_id,
        issued_at="2026-07-28T00:10:00+00:00",
        expires_at="2026-07-28T00:40:00+00:00",
    )
    drifted = _grant(
        grant_id="grant-preflight-drifted",
        kind=GitHubApiIdentityKind.APP_INSTALLATION,
        token="drifted-preflight-token-canary-293",
        actor_id=spec.actor_id,
        repository_id=spec.repository_id,
        installation_id=spec.installation_id,
        issued_at="2026-07-28T00:20:00+00:00",
        expires_at="2026-07-28T00:50:00+00:00",
    )
    issuer = AppIssuer({spec.reference_id: initial})
    verifier = Verifier()
    verifier.proofs[spec.reference_id] = GitHubApiIdentityProof(
        spec.actor_id,
        spec.repository_id,
        spec.capability_ids,
        initial.permission_ids,
        installation_id=spec.installation_id,
    )
    preflight = PreflightGateway(detail_digests=["1" * 64, "1" * 64, "2" * 64])
    provider = _provider((), StoredSource({}), issuer, verifier, (spec,), preflight)
    reference = OpaqueCredentialReference("github-app", spec.reference_id)
    material = provider.resolve(reference)
    assert material is not None

    issuer.grants[spec.reference_id] = refreshed
    renewed = provider.refresh(reference, material)
    assert renewed is not None
    initial_metadata = dict(material.provider_metadata)
    renewed_metadata = dict(renewed.provider_metadata)
    assert (
        initial_metadata["github_preflight_observed_at"]
        != renewed_metadata["github_preflight_observed_at"]
    )
    assert (
        initial_metadata["github_preflight_evidence_digest"]
        == renewed_metadata["github_preflight_evidence_digest"]
    )

    issuer.grants[spec.reference_id] = drifted
    with pytest.raises(RepoForgeError) as failure:
        provider.refresh(reference, renewed)

    assert failure.value.code is ErrorCode.CREDENTIAL_REFRESH_IDENTITY_MISMATCH
    assert drifted.token.released is True


def test_named_stored_accounts_ignore_wrong_global_active_account_and_isolate_concurrently() -> (
    None
):
    personal = _stored("personal", "personal-login", "user-1", "987654")
    company = _stored("company", "company-login", "user-2", "123456")
    personal_grant = _grant(
        grant_id="grant-personal",
        kind=GitHubApiIdentityKind.STORED_ACCOUNT,
        token="personal-token-canary-111",
        actor_id="user-1",
        repository_id="987654",
    )
    company_grant = _grant(
        grant_id="grant-company",
        kind=GitHubApiIdentityKind.STORED_ACCOUNT,
        token="company-token-canary-222",
        actor_id="user-2",
        repository_id="123456",
    )
    source = StoredSource({"personal": personal_grant, "company": company_grant})
    verifier = Verifier()
    verifier.proofs = {
        "personal": GitHubApiIdentityProof(
            "user-1", "987654", personal.capability_ids, personal_grant.permission_ids
        ),
        "company": GitHubApiIdentityProof(
            "user-2", "123456", company.capability_ids, company_grant.permission_ids
        ),
    }
    provider = _provider((personal, company), source, AppIssuer({}), verifier)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(provider.resolve, OpaqueCredentialReference("gh-account", "personal"))
        second = pool.submit(provider.resolve, OpaqueCredentialReference("gh-account", "company"))
        personal_material = first.result()
        company_material = second.result()

    assert source.active_login == "wrong"
    assert sorted(source.calls) == ["company-login", "personal-login"]
    assert personal_material is not None and personal_material.profile_id == "personal"
    assert company_material is not None and company_material.profile_id == "company"
    assert personal_material.actor_id == "user-1"
    assert company_material.actor_id == "user-2"
    assert personal_material.environment[0].value.reveal() == "personal-token-canary-111"
    assert company_material.environment[0].value.reveal() == "company-token-canary-222"


def test_gh_cli_stored_account_source_uses_explicit_user_without_switch_or_ambient_auth() -> None:
    token = "stored-source-token-canary-333"
    executor = IsolatedExecutor(CommandResult(("gh",), "/repo", 0, token + "\n", ""))
    source = GhCliStoredAccountTokenSource(
        executor, cwd=Path("/repo"), clock=FixedClock("2026-07-28T00:00:00+00:00")
    )

    grant = source.issue(_stored("personal", "personal-login", "user-1", "987654"))

    call = executor.calls[0]
    assert call["mode"] == "secret_text"
    assert call["argv"] == (
        "gh",
        "auth",
        "token",
        "--hostname",
        "github.com",
        "--user",
        "personal-login",
    )
    assert "switch" not in call["argv"]
    environment = call["environment"]
    assert environment == {
        "HOME": "/home/demo",
        "PATH": "/safe/bin",
        "GH_PROMPT_DISABLED": "1",
        "GIT_TERMINAL_PROMPT": "0",
    }
    assert grant.token.reveal() == token
    assert token not in repr(grant)
    assert token not in json.dumps(grant.safe_payload(), sort_keys=True)


def test_app_installation_is_repository_scoped_minimally_permissioned_and_revoked_on_release() -> (
    None
):
    spec = _app()
    grant = _grant(
        grant_id="grant-app",
        kind=GitHubApiIdentityKind.APP_INSTALLATION,
        token="app-token-canary-444",
        actor_id=spec.actor_id,
        repository_id=spec.repository_id,
        installation_id=spec.installation_id,
    )
    issuer = AppIssuer({spec.reference_id: grant})
    verifier = Verifier()
    verifier.proofs[spec.reference_id] = GitHubApiIdentityProof(
        spec.actor_id,
        spec.repository_id,
        spec.capability_ids,
        grant.permission_ids,
        installation_id=spec.installation_id,
    )
    provider = _provider((), StoredSource({}), issuer, verifier, (spec,))

    material = provider.resolve(OpaqueCredentialReference("github-app", spec.reference_id))

    assert material is not None
    assert material.profile_id == spec.profile_id
    assert material.actor_class is ActorClass.AUTONOMOUS_AGENT
    assert material.actor_id == spec.actor_id
    assert dict(material.provider_metadata)["installation_id"] == spec.installation_id
    assert dict(material.provider_metadata)["repository_id"] == spec.repository_id
    assert material.capability_ids == spec.capability_ids
    provider.release(material)
    assert issuer.revoked == ["grant-app"]
    assert grant.token.released is True


def test_github_api_material_binds_only_safe_digest_and_metadata_into_auth_lease() -> None:
    spec = _app()
    token = "lease-token-canary-121212"
    grant = _grant(
        grant_id="grant-lease",
        kind=GitHubApiIdentityKind.APP_INSTALLATION,
        token=token,
        actor_id=spec.actor_id,
        repository_id=spec.repository_id,
        installation_id=spec.installation_id,
    )
    issuer = AppIssuer({spec.reference_id: grant})
    verifier = Verifier()
    verifier.proofs[spec.reference_id] = GitHubApiIdentityProof(
        spec.actor_id,
        spec.repository_id,
        spec.capability_ids,
        grant.permission_ids,
        installation_id=spec.installation_id,
    )
    provider = _provider((), StoredSource({}), issuer, verifier, (spec,))
    reference = OpaqueCredentialReference("github-app", spec.reference_id)
    material = provider.resolve(reference)
    assert material is not None

    lease = github_api_auth_lease(
        material,
        reference,
        lease_id="lease-company-app",
        config_revision="a" * 64,
        policy_revision="b" * 64,
    )
    encoded = json.dumps(lease.payload(), sort_keys=True)

    assert lease.material_digest == grant.token_digest()
    assert dict(lease.provider_metadata)["installation_id"] == spec.installation_id
    assert dict(lease.provider_metadata)["repository_id"] == spec.repository_id
    assert token not in encoded
    assert lease.credential_ref == reference


@pytest.mark.parametrize(
    ("proof_override", "code"),
    [
        ({"actor_id": "wrong-user"}, ErrorCode.GITHUB_API_ACTOR_MISMATCH),
        ({"repository_id": "999999"}, ErrorCode.GITHUB_API_REPOSITORY_MISMATCH),
        ({"permission_ids": ("contents:read",)}, ErrorCode.GITHUB_API_PERMISSION_DENIED),
        ({"sso_authorized": False}, ErrorCode.GITHUB_SSO_AUTHORIZATION_REQUIRED),
        ({"approved": False}, ErrorCode.GITHUB_INSTALLATION_APPROVAL_REQUIRED),
        ({"revoked": True}, ErrorCode.GITHUB_TOKEN_REVOKED),
    ],
)
def test_identity_scope_permission_sso_approval_and_revocation_fail_typed(
    proof_override: dict[str, object], code: ErrorCode
) -> None:
    spec = _app()
    grant = _grant(
        grant_id="grant-app",
        kind=GitHubApiIdentityKind.APP_INSTALLATION,
        token="failure-token-canary-555",
        actor_id=spec.actor_id,
        repository_id=spec.repository_id,
        installation_id=spec.installation_id,
    )
    base = {
        "actor_id": spec.actor_id,
        "repository_id": spec.repository_id,
        "capability_ids": spec.capability_ids,
        "permission_ids": grant.permission_ids,
        "installation_id": spec.installation_id,
        "sso_authorized": True,
        "approved": True,
        "revoked": False,
    }
    base.update(proof_override)
    verifier = Verifier()
    verifier.proofs[spec.reference_id] = GitHubApiIdentityProof(**base)  # type: ignore[arg-type]
    provider = _provider(
        (), StoredSource({}), AppIssuer({spec.reference_id: grant}), verifier, (spec,)
    )

    with pytest.raises(RepoForgeError) as failure:
        provider.resolve(OpaqueCredentialReference("github-app", spec.reference_id))

    assert failure.value.code is code
    assert "failure-token-canary-555" not in str(failure.value)
    assert grant.token.released is True


def test_refresh_preserves_installation_identity_and_provider_outage_is_secret_safe() -> None:
    spec = _app()
    initial = _grant(
        grant_id="grant-initial",
        kind=GitHubApiIdentityKind.APP_INSTALLATION,
        token="initial-app-token-canary-666",
        actor_id=spec.actor_id,
        repository_id=spec.repository_id,
        installation_id=spec.installation_id,
    )
    refreshed = _grant(
        grant_id="grant-refreshed",
        kind=GitHubApiIdentityKind.APP_INSTALLATION,
        token="refreshed-app-token-canary-777",
        actor_id=spec.actor_id,
        repository_id=spec.repository_id,
        installation_id=spec.installation_id,
    )
    issuer = AppIssuer({spec.reference_id: initial})
    verifier = Verifier()
    verifier.proofs[spec.reference_id] = GitHubApiIdentityProof(
        spec.actor_id,
        spec.repository_id,
        spec.capability_ids,
        initial.permission_ids,
        installation_id=spec.installation_id,
    )
    provider = _provider((), StoredSource({}), issuer, verifier, (spec,))
    material = provider.resolve(OpaqueCredentialReference("github-app", spec.reference_id))
    assert material is not None

    issuer.grants[spec.reference_id] = refreshed
    renewed = provider.refresh(OpaqueCredentialReference("github-app", spec.reference_id), material)
    assert renewed is not None
    assert renewed.actor_id == material.actor_id
    assert renewed.target_id == material.target_id
    assert renewed.capability_ids == material.capability_ids
    assert renewed.material_digest != material.material_digest

    issuer.failure = RuntimeError("provider outage token=must-not-leak")
    with pytest.raises(RepoForgeError) as outage:
        provider.refresh(OpaqueCredentialReference("github-app", spec.reference_id), renewed)
    assert outage.value.code is ErrorCode.GITHUB_PROVIDER_UNAVAILABLE
    assert "must-not-leak" not in str(outage.value)
    assert outage.value.__context__ is None


def test_gh_cli_app_issuer_mints_selected_permissions_and_revokes_with_exact_tokens() -> None:
    spec = _app()
    token = "issued-app-token-canary-888"
    issue_payload = {
        "token": token,
        "expires_at": "2026-07-28T01:00:00Z",
        "permissions": {"contents": "read", "issues": "write", "pull_requests": "write"},
    }
    executor = IsolatedExecutor(
        [
            CommandResult(("gh",), "/repo", 0, json.dumps(issue_payload), ""),
            CommandResult(("gh",), "/repo", 0, "", ""),
        ]
    )
    signer = JwtSigner()
    issuer = GhCliGitHubAppInstallationTokenIssuer(
        executor,
        cwd=Path("/repo"),
        clock=FixedClock("2026-07-28T00:00:00+00:00"),
        signer=signer,
    )

    grant = issuer.issue(spec)

    issue_call = executor.calls[0]
    assert issue_call["mode"] == "secret_json"
    assert issue_call["argv"][:7] == (
        "gh",
        "api",
        "--hostname",
        "github.com",
        "--method",
        "POST",
        "app/installations/installation-84/access_tokens",
    )
    assert f"repository_ids[]={spec.repository_id}" in issue_call["argv"]
    assert "permissions[issues]=write" in issue_call["argv"]
    assert issue_call["environment"]["GH_TOKEN"] == signer.value
    assert issue_call["secrets"] == (signer.value,)
    assert signer.issued[0].released is True
    assert grant.token.reveal() == token
    assert grant.permission_ids == spec.permission_ids

    issuer.revoke(grant)

    revoke_call = executor.calls[1]
    assert revoke_call["argv"] == (
        "gh",
        "api",
        "--hostname",
        "github.com",
        "--method",
        "DELETE",
        "installation/token",
    )
    assert revoke_call["environment"]["GH_TOKEN"] == token
    assert revoke_call["secrets"] == (token,)
    assert grant.token.released is True


def test_gh_cli_verifier_collects_independent_actor_and_repository_proof() -> None:
    spec = _stored("personal", "personal-login", "1001", "987654")
    grant = _grant(
        grant_id="grant-proof",
        kind=GitHubApiIdentityKind.STORED_ACCOUNT,
        token="proof-token-canary-999",
        actor_id=spec.actor_id,
        repository_id=spec.repository_id,
        permissions=(),
    )
    executor = IsolatedExecutor(
        [
            CommandResult(
                ("gh",), "/repo", 0, json.dumps({"id": 1001, "login": "personal-login"}), ""
            ),
            CommandResult(("gh",), "/repo", 0, json.dumps({"id": 987654}), ""),
        ]
    )
    verifier = GhCliGitHubApiIdentityVerifier(executor, cwd=Path("/repo"))

    proof = verifier.verify_stored_account(spec, grant)

    assert proof.actor_id == "1001"
    assert proof.repository_id == "987654"
    assert tuple(call["argv"][-1] for call in executor.calls) == (
        "user",
        "repositories/987654",
    )
    for call in executor.calls:
        assert call["environment"]["GH_TOKEN"] == "proof-token-canary-999"
        assert call["secrets"] == ("proof-token-canary-999",)


def test_subprocess_secret_capture_never_returns_or_persists_raw_output(tmp_path: Path) -> None:
    text_token = "subprocess-text-token-canary-131313"
    json_token = "subprocess-json-token-canary-141414"
    executor = SubprocessCommandExecutor(ServerConfig(tmp_path / "workspaces", tmp_path / "state"))
    text_script = tmp_path / "emit_text_secret.py"
    text_script.write_text(f"print({text_token!r})\n", encoding="utf-8")
    json_script = tmp_path / "emit_json_secret.py"
    json_script.write_text(
        "import json\n"
        f"print(json.dumps({{'token': {json_token!r}, 'expires_at': '2026-07-28T01:00:00Z'}}))\n",
        encoding="utf-8",
    )
    failure_script = tmp_path / "emit_secret_and_fail.py"
    failure_script.write_text(
        f"import sys\nprint({text_token!r})\nsys.stderr.write({json_token!r})\nsys.exit(7)\n",
        encoding="utf-8",
    )

    text_secret = executor.run_secret_text(
        (sys.executable, str(text_script)),
        cwd=tmp_path,
        environment={},
    )
    payload, json_secret = executor.run_secret_json(
        (sys.executable, str(json_script)),
        cwd=tmp_path,
        environment={},
        secrets=(),
        field="token",
    )

    assert text_secret.reveal() == text_token
    assert json_secret.reveal() == json_token
    assert payload == {"expires_at": "2026-07-28T01:00:00Z"}
    assert text_token not in repr(text_secret)
    assert json_token not in json.dumps(payload, sort_keys=True)

    with pytest.raises(RepoForgeError) as failure:
        executor.run_secret_text(
            (sys.executable, str(failure_script)),
            cwd=tmp_path,
            environment={},
        )
    assert failure.value.code is ErrorCode.COMMAND_FAILED
    assert text_token not in str(failure.value)
    assert json_token not in str(failure.value)
    assert not (tmp_path / "state" / "failure-output-artifacts").exists()

    text_secret.release()
    json_secret.release()
