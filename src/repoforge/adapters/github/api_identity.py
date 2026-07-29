"""Operation-scoped GitHub API repository-auth material providers."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from threading import Lock
from typing import Any, Protocol

from ...domain.errors import ErrorCode, RepoForgeError
from ...domain.github_api_identity import (
    GitHubApiIdentityKind,
    GitHubApiIdentityProof,
    GitHubApiTokenGrant,
    GitHubAppInstallationSpec,
    StoredGhAccountSpec,
)
from ...domain.github_capability_preflight import (
    GitHubCapabilityPreflightReport,
    GitHubCapabilityPreflightRequest,
    GitHubOperationCapability,
    authorize_github_capabilities,
)
from ...domain.repository_auth_broker import (
    AuthEnvironmentBinding,
    AuthMaterial,
    AuthMaterialState,
    EphemeralSecret,
    ProcessAuthContext,
)
from ...domain.repository_identity import (
    ActorClass,
    AuthLease,
    AuthLeaseState,
    AuthTargetKind,
    OpaqueCredentialReference,
    RepositoryProvider,
)
from ...ports.command import CommandResult
from ...ports.github_api_token import (
    GitHubApiIdentityVerifier,
    GitHubAppInstallationTokenIssuer,
    GitHubAppJwtSigner,
    StoredGhAccountTokenSource,
)
from ...ports.github_capability_preflight import GitHubCapabilityPreflightGateway

_SAFE_ENVIRONMENT_KEYS = ("HOME", "PATH", "LANG", "LC_ALL")
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_REFRESHABLE_PREFLIGHT_METADATA = frozenset({"github_preflight_observed_at"})


class _Clock(Protocol):
    def now_iso(self) -> str: ...


class _IsolatedExecutor(Protocol):
    def environment(self, extra: Mapping[str, str] | None = None) -> dict[str, str]: ...

    def run_isolated(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        environment: Mapping[str, str],
        secrets: Sequence[str],
        input_text: str | None = None,
        timeout: int | None = None,
        check: bool = True,
        output_limit: int | None = None,
        cancel_token: Any | None = None,
    ) -> CommandResult: ...

    def run_secret_text(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        environment: Mapping[str, str],
        secrets: Sequence[str] = (),
        timeout: int | None = None,
        max_bytes: int = 100_000,
        cancel_token: Any | None = None,
    ) -> EphemeralSecret: ...

    def run_secret_json(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        environment: Mapping[str, str],
        secrets: Sequence[str],
        field: str,
        timeout: int | None = None,
        max_bytes: int = 1_000_000,
        cancel_token: Any | None = None,
    ) -> tuple[dict[str, object], EphemeralSecret]: ...


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return parsed


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _safe_environment(executor: _IsolatedExecutor) -> dict[str, str]:
    inherited = executor.environment()
    environment = {
        key: inherited[key]
        for key in _SAFE_ENVIRONMENT_KEYS
        if key in inherited and isinstance(inherited[key], str)
    }
    environment["GH_PROMPT_DISABLED"] = "1"
    environment["GIT_TERMINAL_PROMPT"] = "0"
    return environment


def _provider_error(code: ErrorCode, message: str, *, retryable: bool = False) -> RepoForgeError:
    return RepoForgeError(
        message,
        code=code,
        retryable=retryable,
        unchanged_state=("No GitHub API operation was admitted.",),
    )


def _revision(value: str, field_name: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a lowercase SHA-256")
    return value


def _refresh_metadata_identity(
    values: tuple[tuple[str, str], ...],
) -> tuple[tuple[str, str], ...]:
    return tuple(
        (key, value) for key, value in values if key not in _REFRESHABLE_PREFLIGHT_METADATA
    )


def _release_grant(
    grant: GitHubApiTokenGrant,
    *,
    app_issuer: GitHubAppInstallationTokenIssuer | None = None,
) -> None:
    if app_issuer is not None and grant.kind is GitHubApiIdentityKind.APP_INSTALLATION:
        try:
            app_issuer.revoke(grant)
        except Exception:
            grant.token.release()
    else:
        grant.token.release()


def github_api_auth_lease(
    material: AuthMaterial,
    reference: OpaqueCredentialReference,
    *,
    lease_id: str,
    config_revision: str,
    policy_revision: str,
) -> AuthLease:
    """Bind safe GitHub API grant identity into an operation-scoped lease."""

    return AuthLease(
        lease_id=lease_id,
        profile_id=material.profile_id,
        provider=RepositoryProvider.GITHUB,
        repository_id=material.target_id,
        target_kind=material.target_kind,
        target_id=material.target_id,
        actor_id=material.actor_id,
        credential_ref=reference,
        issued_at=material.issued_at,
        expires_at=material.expires_at,
        state=(
            AuthLeaseState.REVOKED
            if material.state is AuthMaterialState.REVOKED
            else AuthLeaseState.ACTIVE
        ),
        config_revision=config_revision,
        policy_revision=policy_revision,
        material_digest=material.material_digest,
        provider_metadata=material.provider_metadata,
    )


class GitHubApiAuthProvider:
    """Resolve reviewed stored-account or App references without global account switching."""

    def __init__(
        self,
        *,
        stored_accounts: tuple[StoredGhAccountSpec, ...],
        app_installations: tuple[GitHubAppInstallationSpec, ...],
        stored_source: StoredGhAccountTokenSource,
        app_issuer: GitHubAppInstallationTokenIssuer,
        verifier: GitHubApiIdentityVerifier,
        capability_preflight: GitHubCapabilityPreflightGateway,
        cwd: Path,
        config_revision: str,
        policy_revision: str,
    ) -> None:
        self._stored_accounts = {item.reference_id: item for item in stored_accounts}
        self._app_installations = {item.reference_id: item for item in app_installations}
        if len(self._stored_accounts) != len(stored_accounts):
            raise ValueError("duplicate stored GitHub account reference")
        if len(self._app_installations) != len(app_installations):
            raise ValueError("duplicate GitHub App installation reference")
        overlap = set(self._stored_accounts).intersection(self._app_installations)
        if overlap:
            raise ValueError("GitHub API reference IDs must be unique across identity kinds")
        self._stored_source = stored_source
        self._app_issuer = app_issuer
        self._verifier = verifier
        self._capability_preflight = capability_preflight
        if not isinstance(cwd, Path) or not cwd.is_absolute():
            raise ValueError("cwd must be an absolute Path")
        self._cwd = cwd
        self._config_revision = _revision(config_revision, "config_revision")
        self._policy_revision = _revision(policy_revision, "policy_revision")
        self._issued: dict[str, GitHubApiTokenGrant] = {}
        self._lock = Lock()

    def _issue_stored(
        self, spec: StoredGhAccountSpec
    ) -> tuple[GitHubApiTokenGrant, GitHubApiIdentityProof]:
        failed = False
        try:
            grant = self._stored_source.issue(spec)
        except Exception:
            grant = None
            failed = True
        if failed or grant is None:
            raise _provider_error(
                ErrorCode.GITHUB_PROVIDER_UNAVAILABLE,
                "Named GitHub account token resolution failed.",
                retryable=True,
            )
        verify_failed = False
        try:
            proof = self._verifier.verify_stored_account(spec, grant)
        except Exception:
            proof = None
            verify_failed = True
        if verify_failed or proof is None:
            _release_grant(grant)
            raise _provider_error(
                ErrorCode.GITHUB_PROVIDER_UNAVAILABLE,
                "Named GitHub account identity verification failed.",
                retryable=True,
            )
        return grant, proof

    def _issue_app(
        self, spec: GitHubAppInstallationSpec
    ) -> tuple[GitHubApiTokenGrant, GitHubApiIdentityProof]:
        failed = False
        try:
            grant = self._app_issuer.issue(spec)
        except Exception:
            grant = None
            failed = True
        if failed or grant is None:
            raise _provider_error(
                ErrorCode.GITHUB_PROVIDER_UNAVAILABLE,
                "GitHub App installation token issuance failed.",
                retryable=True,
            )
        verify_failed = False
        try:
            proof = self._verifier.verify_app_installation(spec, grant)
        except Exception:
            proof = None
            verify_failed = True
        if verify_failed or proof is None:
            _release_grant(grant, app_issuer=self._app_issuer)
            raise _provider_error(
                ErrorCode.GITHUB_PROVIDER_UNAVAILABLE,
                "GitHub App installation identity verification failed.",
                retryable=True,
            )
        return grant, proof

    def _validate(
        self,
        *,
        grant: GitHubApiTokenGrant,
        proof: GitHubApiIdentityProof,
        actor_id: str,
        repository_id: str,
        capability_ids: tuple[str, ...],
        permission_ids: tuple[str, ...] | None,
        installation_id: str | None,
    ) -> None:
        def reject(code: ErrorCode, message: str) -> None:
            _release_grant(
                grant,
                app_issuer=(
                    self._app_issuer
                    if grant.kind is GitHubApiIdentityKind.APP_INSTALLATION
                    else None
                ),
            )
            raise _provider_error(code, message)

        if grant.revoked or proof.revoked:
            reject(ErrorCode.GITHUB_TOKEN_REVOKED, "GitHub API token was revoked.")
        if not grant.sso_authorized or not proof.sso_authorized:
            reject(
                ErrorCode.GITHUB_SSO_AUTHORIZATION_REQUIRED,
                "GitHub organization SSO authorization is required.",
            )
        if not grant.approved or not proof.approved:
            reject(
                ErrorCode.GITHUB_INSTALLATION_APPROVAL_REQUIRED,
                "GitHub App installation approval is required.",
            )
        if grant.actor_id != actor_id or proof.actor_id != actor_id:
            reject(ErrorCode.GITHUB_API_ACTOR_MISMATCH, "Observed GitHub API actor mismatched.")
        if grant.repository_id != repository_id or proof.repository_id != repository_id:
            reject(
                ErrorCode.GITHUB_API_REPOSITORY_MISMATCH,
                "Observed GitHub API repository mismatched.",
            )
        if installation_id is not None and (
            grant.installation_id != installation_id or proof.installation_id != installation_id
        ):
            reject(ErrorCode.GITHUB_API_ACTOR_MISMATCH, "Observed App installation mismatched.")
        expected_capabilities = set(capability_ids)
        if (
            set(grant.capability_ids) != expected_capabilities
            or set(proof.capability_ids) != expected_capabilities
        ):
            reject(
                ErrorCode.GITHUB_API_PERMISSION_DENIED,
                "GitHub API capability proof did not match the reviewed ceiling.",
            )
        if permission_ids is not None and (
            set(grant.permission_ids) != set(permission_ids)
            or set(proof.permission_ids) != set(permission_ids)
        ):
            reject(
                ErrorCode.GITHUB_API_PERMISSION_DENIED,
                "GitHub App permissions were not the reviewed minimal set.",
            )

    def _material(
        self,
        *,
        grant: GitHubApiTokenGrant,
        profile_id: str,
        actor_class: ActorClass,
        host: str,
        preflight: GitHubCapabilityPreflightReport,
    ) -> AuthMaterial:
        digest = grant.token_digest()
        material_id = (
            "github-api-"
            + hashlib.sha256(
                f"{grant.kind.value}\0{grant.grant_id}\0{digest}".encode()
            ).hexdigest()[:24]
        )
        metadata: list[tuple[str, str]] = [
            ("github_kind", grant.kind.value),
            ("github_host", host),
            ("repository_id", grant.repository_id),
            ("github_preflight_evidence_digest", preflight.evidence_digest),
            ("github_capability_digest", preflight.capability_digest),
            ("github_permission_digest", preflight.permission_digest),
            ("github_preflight_observed_at", preflight.observed_at),
            ("config_revision", preflight.config_revision),
            ("policy_revision", preflight.policy_revision),
        ]
        if grant.installation_id is not None:
            metadata.append(("installation_id", grant.installation_id))
        material = AuthMaterial(
            material_id=material_id,
            profile_id=profile_id,
            actor_class=actor_class,
            target_kind=AuthTargetKind.REPOSITORY,
            target_id=grant.repository_id,
            capability_ids=grant.capability_ids,
            issued_at=grant.issued_at,
            expires_at=grant.expires_at,
            state=AuthMaterialState.REVOKED if grant.revoked else AuthMaterialState.ACTIVE,
            actor_id=grant.actor_id,
            material_digest=digest,
            provider_metadata=tuple(metadata),
            environment=(AuthEnvironmentBinding("GH_TOKEN", grant.token),),
        )
        with self._lock:
            self._issued[material_id] = grant
        return material

    def _preflight(
        self,
        *,
        grant: GitHubApiTokenGrant,
        profile_id: str,
        host: str,
    ) -> GitHubCapabilityPreflightReport:
        token = grant.token.reveal()
        auth_context = ProcessAuthContext(
            profile_id=profile_id,
            material_id=grant.grant_id,
            target_kind=AuthTargetKind.REPOSITORY,
            target_id=grant.repository_id,
            environment=(("GH_TOKEN", token),),
            _secret_values=(token,),
        )
        request = GitHubCapabilityPreflightRequest(
            host=host,
            actor_id=grant.actor_id,
            repository_id=grant.repository_id,
            installation_id=grant.installation_id,
            capability_ids=tuple(
                GitHubOperationCapability(value) for value in grant.capability_ids
            ),
            permission_ids=grant.permission_ids,
            config_revision=self._config_revision,
            policy_revision=self._policy_revision,
            observed_at=grant.issued_at,
        )
        app_issuer = (
            self._app_issuer if grant.kind is GitHubApiIdentityKind.APP_INSTALLATION else None
        )
        try:
            report = self._capability_preflight.preflight(
                self._cwd,
                request,
                auth_context,
            )
            if (
                report.host != request.host
                or report.actor_id != request.actor_id
                or report.repository_id != request.repository_id
                or report.installation_id != request.installation_id
                or set(report.capability_ids) != set(request.capability_ids)
                or set(report.permission_ids) != set(request.permission_ids)
                or report.config_revision != request.config_revision
                or report.policy_revision != request.policy_revision
                or report.observed_at != request.observed_at
            ):
                raise _provider_error(
                    ErrorCode.GITHUB_API_PERMISSION_DENIED,
                    "GitHub capability preflight changed a reviewed identity field.",
                )
            return authorize_github_capabilities(report)
        except RepoForgeError:
            _release_grant(grant, app_issuer=app_issuer)
            raise
        except Exception:
            _release_grant(grant, app_issuer=app_issuer)
            raise _provider_error(
                ErrorCode.GITHUB_PROVIDER_UNAVAILABLE,
                "GitHub capability preflight failed without usable evidence.",
                retryable=True,
            ) from None

    def resolve(self, reference: OpaqueCredentialReference) -> AuthMaterial | None:
        if reference.scheme == "gh-account":
            stored_spec = self._stored_accounts.get(reference.reference_id)
            if stored_spec is None:
                return None
            grant, proof = self._issue_stored(stored_spec)
            if grant.kind is not GitHubApiIdentityKind.STORED_ACCOUNT:
                _release_grant(grant)
                raise _provider_error(
                    ErrorCode.GITHUB_API_ACTOR_MISMATCH,
                    "Named GitHub account returned the wrong identity kind.",
                )
            self._validate(
                grant=grant,
                proof=proof,
                actor_id=stored_spec.actor_id,
                repository_id=stored_spec.repository_id,
                capability_ids=stored_spec.capability_ids,
                permission_ids=None,
                installation_id=None,
            )
            preflight = self._preflight(
                grant=grant,
                profile_id=stored_spec.profile_id,
                host=stored_spec.host,
            )
            return self._material(
                grant=grant,
                profile_id=stored_spec.profile_id,
                actor_class=stored_spec.actor_class,
                host=stored_spec.host,
                preflight=preflight,
            )
        if reference.scheme == "github-app":
            app_spec = self._app_installations.get(reference.reference_id)
            if app_spec is None:
                return None
            grant, proof = self._issue_app(app_spec)
            if grant.kind is not GitHubApiIdentityKind.APP_INSTALLATION:
                _release_grant(grant, app_issuer=self._app_issuer)
                raise _provider_error(
                    ErrorCode.GITHUB_API_ACTOR_MISMATCH,
                    "GitHub App issuer returned the wrong identity kind.",
                )
            self._validate(
                grant=grant,
                proof=proof,
                actor_id=app_spec.actor_id,
                repository_id=app_spec.repository_id,
                capability_ids=app_spec.capability_ids,
                permission_ids=app_spec.permission_ids,
                installation_id=app_spec.installation_id,
            )
            preflight = self._preflight(
                grant=grant,
                profile_id=app_spec.profile_id,
                host=app_spec.host,
            )
            return self._material(
                grant=grant,
                profile_id=app_spec.profile_id,
                actor_class=ActorClass.AUTONOMOUS_AGENT,
                host=app_spec.host,
                preflight=preflight,
            )
        return None

    def refresh(
        self,
        reference: OpaqueCredentialReference,
        previous: AuthMaterial,
    ) -> AuthMaterial | None:
        material = self.resolve(reference)
        if material is None:
            return None
        if (
            material.profile_id != previous.profile_id
            or material.actor_class is not previous.actor_class
            or material.actor_id != previous.actor_id
            or material.target_kind is not previous.target_kind
            or material.target_id != previous.target_id
            or material.capability_ids != previous.capability_ids
            or _refresh_metadata_identity(material.provider_metadata)
            != _refresh_metadata_identity(previous.provider_metadata)
        ):
            self.release(material)
            raise _provider_error(
                ErrorCode.CREDENTIAL_REFRESH_IDENTITY_MISMATCH,
                "Refreshed GitHub API token changed a locked identity field.",
            )
        return material

    def release(self, material: AuthMaterial) -> None:
        with self._lock:
            grant = self._issued.pop(material.material_id, None)
        if grant is not None:
            _release_grant(
                grant,
                app_issuer=(
                    self._app_issuer
                    if grant.kind is GitHubApiIdentityKind.APP_INSTALLATION
                    else None
                ),
            )
        material.release()


class GhCliStoredAccountTokenSource:
    """Read one explicitly named stored gh account; never switch the active account."""

    def __init__(self, executor: _IsolatedExecutor, *, cwd: Path, clock: _Clock) -> None:
        self._executor = executor
        self._cwd = cwd
        self._clock = clock

    def issue(self, spec: StoredGhAccountSpec) -> GitHubApiTokenGrant:
        token = self._executor.run_secret_text(
            [
                "gh",
                "auth",
                "token",
                "--hostname",
                spec.host,
                "--user",
                spec.login,
            ],
            cwd=self._cwd,
            environment=_safe_environment(self._executor),
            secrets=(),
            max_bytes=100_000,
        )
        issued = _parse_timestamp(self._clock.now_iso())
        digest = hashlib.sha256(token.reveal().encode()).hexdigest()
        return GitHubApiTokenGrant(
            grant_id=f"gh-account-{digest[:24]}",
            kind=GitHubApiIdentityKind.STORED_ACCOUNT,
            token=token,
            actor_id=spec.actor_id,
            repository_id=spec.repository_id,
            capability_ids=spec.capability_ids,
            permission_ids=(),
            issued_at=_iso(issued),
            expires_at=_iso(issued + timedelta(seconds=spec.lease_seconds)),
        )


class GhCliGitHubApiIdentityVerifier:
    """Collect independent live actor/repository proof using the issued token only."""

    def __init__(self, executor: _IsolatedExecutor, *, cwd: Path) -> None:
        self._executor = executor
        self._cwd = cwd

    def _api(self, host: str, grant: GitHubApiTokenGrant, endpoint: str) -> object:
        token = grant.token.reveal()
        environment = _safe_environment(self._executor)
        environment["GH_TOKEN"] = token
        result = self._executor.run_isolated(
            ["gh", "api", "--hostname", host, endpoint],
            cwd=self._cwd,
            environment=environment,
            secrets=(token,),
            output_limit=5_000_000,
        )
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError:
            raise _provider_error(
                ErrorCode.GITHUB_PROVIDER_UNAVAILABLE,
                "GitHub API identity probe returned invalid JSON.",
            ) from None

    def verify_stored_account(
        self, spec: StoredGhAccountSpec, grant: GitHubApiTokenGrant
    ) -> GitHubApiIdentityProof:
        user = self._api(spec.host, grant, "user")
        repository = self._api(spec.host, grant, f"repositories/{spec.repository_id}")
        if not isinstance(user, dict) or not isinstance(repository, dict):
            raise _provider_error(
                ErrorCode.GITHUB_PROVIDER_UNAVAILABLE,
                "GitHub API identity probe returned an unexpected payload.",
            )
        login = user.get("login")
        actor_id = str(user.get("id", ""))
        if spec.actor_id == spec.login:
            actor_id = login if isinstance(login, str) else ""
        repository_id = str(repository.get("id", ""))
        return GitHubApiIdentityProof(
            actor_id=actor_id,
            repository_id=repository_id,
            capability_ids=grant.capability_ids,
            permission_ids=grant.permission_ids,
        )

    def verify_app_installation(
        self, spec: GitHubAppInstallationSpec, grant: GitHubApiTokenGrant
    ) -> GitHubApiIdentityProof:
        payload = self._api(spec.host, grant, "installation/repositories?per_page=100")
        repositories = payload.get("repositories") if isinstance(payload, dict) else None
        observed = {
            str(item.get("id", "")) for item in repositories or [] if isinstance(item, dict)
        }
        repository_id = spec.repository_id if spec.repository_id in observed else "missing"
        return GitHubApiIdentityProof(
            actor_id=spec.actor_id,
            repository_id=repository_id,
            capability_ids=grant.capability_ids,
            permission_ids=grant.permission_ids,
            installation_id=spec.installation_id,
        )


class GhCliGitHubAppInstallationTokenIssuer:
    """Mint and revoke repository-selected installation tokens using an external JWT signer."""

    def __init__(
        self,
        executor: _IsolatedExecutor,
        *,
        cwd: Path,
        clock: _Clock,
        signer: GitHubAppJwtSigner,
    ) -> None:
        self._executor = executor
        self._cwd = cwd
        self._clock = clock
        self._signer = signer
        self._hosts: dict[str, str] = {}
        self._lock = Lock()

    def issue(self, spec: GitHubAppInstallationSpec) -> GitHubApiTokenGrant:
        issued = _parse_timestamp(self._clock.now_iso())
        jwt = self._signer.issue(
            app_id=spec.app_id,
            issued_at=_iso(issued - timedelta(seconds=30)),
            expires_at=_iso(issued + timedelta(minutes=9)),
        )
        jwt_text = jwt.reveal()
        environment = _safe_environment(self._executor)
        environment["GH_TOKEN"] = jwt_text
        argv = [
            "gh",
            "api",
            "--hostname",
            spec.host,
            "--method",
            "POST",
            f"app/installations/{spec.installation_id}/access_tokens",
            "-F",
            f"repository_ids[]={spec.repository_id}",
        ]
        for name, level in spec.permissions:
            argv.extend(["-f", f"permissions[{name}]={level}"])
        try:
            payload, token = self._executor.run_secret_json(
                argv,
                cwd=self._cwd,
                environment=environment,
                secrets=(jwt_text,),
                field="token",
                max_bytes=1_000_000,
            )
        finally:
            jwt.release()
        expires_at = payload.get("expires_at")
        if not isinstance(expires_at, str):
            token.release()
            raise _provider_error(
                ErrorCode.GITHUB_PROVIDER_UNAVAILABLE,
                "GitHub App token endpoint omitted bounded token metadata.",
            )
        permissions = payload.get("permissions")
        permission_ids = (
            tuple(sorted(f"{name}:{level}" for name, level in permissions.items()))
            if isinstance(permissions, dict)
            and all(
                isinstance(name, str) and isinstance(level, str)
                for name, level in permissions.items()
            )
            else spec.permission_ids
        )
        digest = hashlib.sha256(token.reveal().encode()).hexdigest()
        grant = GitHubApiTokenGrant(
            grant_id=f"github-app-{digest[:24]}",
            kind=GitHubApiIdentityKind.APP_INSTALLATION,
            token=token,
            actor_id=spec.actor_id,
            repository_id=spec.repository_id,
            capability_ids=spec.capability_ids,
            permission_ids=permission_ids,
            issued_at=_iso(issued),
            expires_at=expires_at,
            installation_id=spec.installation_id,
        )
        with self._lock:
            self._hosts[grant.grant_id] = spec.host
        return grant

    def revoke(self, grant: GitHubApiTokenGrant) -> None:
        with self._lock:
            host = self._hosts.pop(grant.grant_id, "github.com")
        token = grant.token.reveal()
        environment = _safe_environment(self._executor)
        environment["GH_TOKEN"] = token
        try:
            self._executor.run_isolated(
                ["gh", "api", "--hostname", host, "--method", "DELETE", "installation/token"],
                cwd=self._cwd,
                environment=environment,
                secrets=(token,),
                output_limit=100_000,
            )
        finally:
            grant.token.release()
