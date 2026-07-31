"""Adopt an already-configured local identity as a reviewed RepoForge auth profile.

`inspect()` gathers live evidence and returns a hash-bound plan; `apply()` re-gathers the same
evidence and refuses unless every input still matches. Nothing here switches the active GitHub
account, writes Git or SSH configuration, or reads a credential value: ambient state is only
ever *reported*, so the operator decides what to keep.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ..domain.auth_migration import (
    AuthMigrationChange,
    AuthMigrationChangeKind,
    AuthMigrationFinding,
    AuthMigrationFindingCode,
    AuthMigrationPlan,
    AuthMigrationSeverity,
    NamedAccountCandidate,
    SshAliasCandidate,
    ambient_token_environment_names,
    build_auth_migration_plan,
    canonical_plan_hash,
)
from ..domain.config_generation import ApprovalEvent, ConfigMutation, sha256_text
from ..domain.errors import ErrorCode, RepoForgeError
from ..domain.github_capability_preflight import GitHubOperationCapability
from ..domain.repository_identity import RecoveryAction, RecoveryActionKind
from ..domain.repository_identity_resolution import RepositoryIdentityObservation
from ..ports.auth_discovery import (
    AmbientAuthConflictReader,
    NamedAccountDiscovery,
    SshAliasDiscovery,
)
from ..ports.clock import Clock
from ..ports.configuration import ConfigurationStore
from ..ports.ids import IdGenerator
from .configuration.document import apply_auth_profiles, parse_resolved, render_resolved
from .configuration.source import (
    SourceAuthProfile,
    SourceConfiguration,
    parse_source,
    render_source,
)

#: The minimal capability set a migrated human profile is proposed with. Widening it is a
#: separate reviewed configuration change, not something migration decides.
_MIGRATED_CAPABILITIES = (
    GitHubOperationCapability.CONTENTS_READ.value,
    GitHubOperationCapability.CONTENTS_WRITE.value,
    GitHubOperationCapability.PULL_REQUESTS_WRITE.value,
)
#: Git configuration keys read read-only to report what would otherwise act ambiently.
_HELPER_KEYS = ("credential.helper",)
_AUTHOR_KEYS = ("user.email", "user.name")
_SIGNER_KEYS = ("user.signingkey", "commit.gpgsign", "gpg.format")
#: Git's own truthy spellings for an enabled commit signer.
_SIGNING_TRUE = {"true", "yes", "on", "1"}
_REMOTE_KEY = "remote.origin.url"
_LOGIN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
_NON_ENVIRONMENT = re.compile(r"[^A-Z0-9]+")
_REMOTE_TARGET = re.compile(
    r"^(?:https://|ssh://(?:[^@/]+@)?|(?:[^@/]+@))?(?P<host>[A-Za-z0-9.-]+)[:/]"
    r"(?P<owner>[A-Za-z0-9_.-]+)/(?P<repository>[A-Za-z0-9_.-]+?)(?:\.git)?/?$"
)


class ObserveRepository(Protocol):
    """Live stable-identity observation under one named account or local-only discovery."""

    def __call__(self, repo_id: str, login: str | None) -> RepositoryIdentityObservation: ...


def _failure(code: ErrorCode, message: str, *, next_action: str) -> RepoForgeError:
    return RepoForgeError(
        message,
        code=code,
        retryable=False,
        unchanged_state=("configuration", "runtime"),
        safe_next_action=next_action,
    )


def _token_environment(login: str) -> str:
    """Derive the deterministic environment name a migrated HTTPS profile would read."""

    suffix = _NON_ENVIRONMENT.sub("_", login.upper()).strip("_")
    return f"REPOFORGE_GH_TOKEN_{suffix}"


def _reference_fingerprint(*parts: str) -> str:
    """Fingerprint *which* credential is pinned, derived from its reference, never its value."""

    return hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class _Gathered:
    """One consistent read of every input a plan depends on."""

    source: SourceConfiguration
    source_sha256: str
    generation: int
    repo_id: str
    repo_path: Path
    observation: RepositoryIdentityObservation
    candidates: tuple[NamedAccountCandidate, ...]
    verified: NamedAccountCandidate | None
    ssh: SshAliasCandidate | None
    plan: AuthMigrationPlan
    already_migrated: bool


class AuthMigrationService:
    """Inspect and apply migration plans for one repository at a time."""

    def __init__(
        self,
        *,
        store: ConfigurationStore,
        clock: Clock,
        ids: IdGenerator,
        accounts: NamedAccountDiscovery,
        ssh: SshAliasDiscovery,
        ambient: AmbientAuthConflictReader,
        observe: ObserveRepository,
        host: str = "github.com",
        ssh_alias: str | None = None,
    ) -> None:
        self._store = store
        self._clock = clock
        self._ids = ids
        self._accounts = accounts
        self._ssh = ssh
        self._ambient = ambient
        self._observe = observe
        self._host = host
        self._ssh_alias = ssh_alias

    # -- public surface ------------------------------------------------------

    def inspect(self, *, repo_id: str, login: str | None = None) -> AuthMigrationPlan:
        """Report what could be adopted and what a human still has to decide."""

        return self._gather(repo_id, login=login).plan

    def apply(
        self,
        *,
        repo_id: str,
        plan_id: str,
        plan_hash: str,
        actor: str,
        login: str | None = None,
    ) -> dict[str, object]:
        """Re-prove every input, then write exactly one reviewed configuration generation.

        Adopting an identity widens what the runtime may act as, which the capability-delta
        classifier reports as an expansion. The plan hash the operator transcribes after reading
        `rf auth migrate inspect` is therefore recorded as their explicit approval of exactly
        that content; an agent cannot mint one, because a wrong hash is refused above.
        """

        if not isinstance(actor, str) or not actor or len(actor) > 200:
            raise _failure(
                ErrorCode.INPUT_REQUIRED,
                "An approving operator must be recorded for a migration apply.",
                next_action="Re-run the command as the operator who reviewed the plan.",
            )
        gathered = self._gather(repo_id, login=login)
        if not gathered.candidates and not gathered.already_migrated:
            raise _failure(
                ErrorCode.CREDENTIAL_REFERENCE_NOT_FOUND,
                "No locally configured account remains for this repository's provider host.",
                next_action=(
                    "Sign in with `gh auth login --hostname "
                    f"{self._host}` in a terminal, then re-run the migration inspection."
                ),
            )
        if len(gathered.candidates) > 1:
            raise _failure(
                ErrorCode.INPUT_REQUIRED,
                "More than one locally configured account could serve this repository.",
                next_action=(
                    "Ask the operator to declare the intended account explicitly with "
                    "`rf auth import gh --login <login>`."
                ),
            )
        plan = gathered.plan
        if plan.plan_id != plan_id or plan.plan_hash != plan_hash:
            raise _failure(
                ErrorCode.CONFIG_STALE,
                "The migration plan no longer matches the current configuration and evidence.",
                next_action="Re-run `rf auth migrate inspect` and apply the fresh plan.",
            )
        if not plan.ready:
            raise _failure(
                ErrorCode.INPUT_REQUIRED,
                "This migration plan still requires manual remediation.",
                next_action=("Resolve the blocking findings the plan reports, then inspect again."),
            )
        return self._write(gathered, actor=actor)

    # -- gathering -----------------------------------------------------------

    def _gather(self, repo_id: str, *, login: str | None = None) -> _Gathered:
        source_text = self._store.read_source_text()
        source = parse_source(source_text)
        current = self._store.current()
        if current is None:
            raise _failure(
                ErrorCode.CONFIG_STALE,
                "No accepted configuration generation exists to migrate from.",
                next_action="Run `rf setup` before migrating repository identities.",
            )
        repositories = {item.repo_id: item for item in source.repositories}
        repository = repositories.get(repo_id)
        if repository is None:
            raise _failure(
                ErrorCode.NOT_FOUND,
                f"Unknown repository id: {repo_id}",
                next_action="Run `rf config inspect` to list the configured repositories.",
            )
        candidates = self._accounts.candidates(host=self._host)
        if login is not None:
            if _LOGIN.fullmatch(login) is None:
                raise _failure(
                    ErrorCode.CREDENTIAL_REFERENCE_NOT_FOUND,
                    f"The requested login {login!r} is not a bounded GitHub login.",
                    next_action="Run `rf auth import gh` to list the configured accounts.",
                )
            narrowed = tuple(item for item in candidates if item.login == login)
            if len(narrowed) != 1:
                raise _failure(
                    ErrorCode.CREDENTIAL_REFERENCE_NOT_FOUND,
                    f"No locally configured account matches login {login!r} for {self._host}.",
                    next_action="Run `rf auth import gh` to list the configured accounts.",
                )
            candidates = narrowed
        selected_login = candidates[0].login if len(candidates) == 1 else None
        observation = self._observe(repo_id, selected_login)
        repo_path = Path(repository.path)

        findings: list[AuthMigrationFinding] = []
        changes: list[AuthMigrationChange] = []
        already_migrated = any(
            profile.repository_id == observation.repository_id for profile in source.auth_profiles
        )
        if not source.auth_profiles:
            findings.append(
                AuthMigrationFinding(
                    code=AuthMigrationFindingCode.LEGACY_NO_AUTH_PROFILE,
                    severity=AuthMigrationSeverity.INFO,
                    subject=repo_id,
                    detail=(
                        "This configuration declares no auth profiles, so every operation still "
                        "depends on whatever account happens to be active."
                    ),
                    recovery_actions=(RecoveryAction(RecoveryActionKind.RESELECT_PROFILE),),
                )
            )

        verified: NamedAccountCandidate | None = None
        ssh_candidate: SshAliasCandidate | None = None
        transport_kind: str | None = None

        if already_migrated:
            existing = next(
                (
                    profile
                    for profile in source.auth_profiles
                    if profile.repository_id == observation.repository_id
                ),
                None,
            )
            transport_kind = existing.transport_kind if existing is not None else None
        elif not candidates:
            findings.append(
                AuthMigrationFinding(
                    code=AuthMigrationFindingCode.NAMED_ACCOUNT_MISSING,
                    severity=AuthMigrationSeverity.INFO,
                    subject=self._host,
                    detail=(
                        "No account is configured locally for this provider host, so there is "
                        "nothing to adopt yet."
                    ),
                    recovery_actions=(RecoveryAction(RecoveryActionKind.REAUTHORIZE),),
                )
            )
        elif len(candidates) > 1:
            findings.append(
                AuthMigrationFinding(
                    code=AuthMigrationFindingCode.NAMED_ACCOUNT_AMBIGUOUS,
                    severity=AuthMigrationSeverity.BLOCKING,
                    subject=self._host,
                    detail=(
                        "More than one account is configured locally ("
                        + ", ".join(sorted(item.login for item in candidates))
                        + "); which one this repository should use is not derivable."
                    ),
                    recovery_actions=(RecoveryAction(RecoveryActionKind.RESELECT_PROFILE),),
                )
            )
            changes.append(
                AuthMigrationChange(
                    kind=AuthMigrationChangeKind.MANUAL_REMEDIATION,
                    repo_id=repo_id,
                    profile_id="unresolved",
                    summary="Declare which locally configured account this repository should use.",
                )
            )
        else:
            verified = self._accounts.verify(host=self._host, login=candidates[0].login)
            if not verified.active:
                findings.append(
                    AuthMigrationFinding(
                        code=AuthMigrationFindingCode.ACTIVE_ACCOUNT_DIFFERS,
                        severity=AuthMigrationSeverity.INFO,
                        subject=verified.login,
                        detail=(
                            f"{verified.login} is not the globally active account. RepoForge will "
                            "use it explicitly and will not switch the active account."
                        ),
                    )
                )
            ssh_candidate = self._ssh_candidate(observation, findings)
            transport_kind = "ssh" if ssh_candidate is not None else "https"
            changes.extend(self._proposed_changes(repo_id, observation, verified, ssh_candidate))

        findings.extend(
            self._ambient_findings(repo_id, repo_path, observation, transport_kind=transport_kind)
        )

        finding_tuple = tuple(findings[:64])
        change_tuple = tuple(changes[:64])
        source_sha256 = sha256_text(source_text)
        plan = build_auth_migration_plan(
            plan_id=self._plan_id(source_sha256, current.generation, finding_tuple, change_tuple),
            source_sha256=source_sha256,
            config_generation=current.generation,
            findings=finding_tuple,
            changes=change_tuple,
        )
        return _Gathered(
            source=source,
            source_sha256=source_sha256,
            generation=current.generation,
            repo_id=repo_id,
            repo_path=repo_path,
            observation=observation,
            candidates=candidates,
            verified=verified,
            ssh=ssh_candidate,
            plan=plan,
            already_migrated=already_migrated,
        )

    def _plan_id(
        self,
        source_sha256: str,
        generation: int,
        findings: tuple[AuthMigrationFinding, ...],
        changes: tuple[AuthMigrationChange, ...],
    ) -> str:
        """Content-address the plan so repeating an inspection is idempotent."""

        digest = canonical_plan_hash(
            source_sha256=source_sha256,
            config_generation=generation,
            findings=findings,
            changes=changes,
        )
        return f"authmig-{digest[:24]}"

    def _ssh_candidate(
        self,
        observation: RepositoryIdentityObservation,
        findings: list[AuthMigrationFinding],
    ) -> SshAliasCandidate | None:
        alias = self._ssh_alias or observation.provider_host
        try:
            candidate = self._ssh.inspect(alias)
        except RepoForgeError:
            findings.append(
                AuthMigrationFinding(
                    code=AuthMigrationFindingCode.SSH_CONFIGURATION_AMBIGUOUS,
                    severity=AuthMigrationSeverity.INFO,
                    subject=alias,
                    detail=(
                        f"The SSH alias {alias} does not resolve to one pinnable identity file, "
                        "so the plan proposes an explicit HTTPS transport instead."
                    ),
                    recovery_actions=(RecoveryAction(RecoveryActionKind.REVIEW_REMOTE),),
                )
            )
            return None
        if candidate.hostname != observation.provider_host:
            findings.append(
                AuthMigrationFinding(
                    code=AuthMigrationFindingCode.SSH_CONFIGURATION_AMBIGUOUS,
                    severity=AuthMigrationSeverity.INFO,
                    subject=alias,
                    detail=(
                        f"The SSH alias {alias} resolves to {candidate.hostname}, not the observed "
                        f"provider host {observation.provider_host}."
                    ),
                    recovery_actions=(RecoveryAction(RecoveryActionKind.REVIEW_REMOTE),),
                )
            )
            return None
        return candidate

    def _ambient_findings(
        self,
        repo_id: str,
        repo_path: Path,
        observation: RepositoryIdentityObservation,
        *,
        transport_kind: str | None,
    ) -> tuple[AuthMigrationFinding, ...]:
        findings: list[AuthMigrationFinding] = []
        ambient_names = ambient_token_environment_names(self._ambient.environment_names())
        if ambient_names:
            findings.append(
                AuthMigrationFinding(
                    code=AuthMigrationFindingCode.AMBIENT_TOKEN_ENVIRONMENT,
                    severity=AuthMigrationSeverity.BLOCKING,
                    subject=repo_id,
                    detail=(
                        "These environment variables would authenticate ambiently and can "
                        "silently override a reviewed profile: " + ", ".join(ambient_names) + "."
                    ),
                    recovery_actions=(RecoveryAction(RecoveryActionKind.REAUTHORIZE),),
                )
            )
        for key in _HELPER_KEYS:
            observed = self._ambient.git_config_values(repo_path, key)
            if observed:
                helper_finding = self._credential_helper_finding(
                    key, observed, transport_kind=transport_kind
                )
                if helper_finding is not None:
                    findings.append(helper_finding)
        for key in _AUTHOR_KEYS:
            observed = self._ambient.git_config_values(repo_path, key)
            if len({value for _origin, value in observed}) > 1:
                findings.append(
                    AuthMigrationFinding(
                        code=AuthMigrationFindingCode.COMMIT_IDENTITY_CONFLICT,
                        severity=AuthMigrationSeverity.BLOCKING,
                        subject=key,
                        detail=(
                            f"{key} has conflicting values across "
                            + ", ".join(origin for origin, _value in observed)
                            + ", so which author a commit would carry is not decidable."
                        ),
                        recovery_actions=(RecoveryAction(RecoveryActionKind.REVIEW_REMOTE),),
                    )
                )
        signer_scopes = [
            (key, observed)
            for key in _SIGNER_KEYS
            if (observed := self._ambient.git_config_values(repo_path, key))
        ]
        if _signing_is_active(signer_scopes):
            findings.append(
                AuthMigrationFinding(
                    code=AuthMigrationFindingCode.COMMIT_SIGNER_CONFLICT,
                    severity=AuthMigrationSeverity.BLOCKING,
                    subject=repo_id,
                    detail=(
                        "Signing is actually enabled ("
                        + ", ".join(key for key, _observed in signer_scopes)
                        + "); a migrated profile must not silently change what signs a commit."
                    ),
                    recovery_actions=(RecoveryAction(RecoveryActionKind.REVIEW_REMOTE),),
                )
            )
        findings.extend(self._remote_findings(repo_path, observation))
        return tuple(findings)

    def _credential_helper_finding(
        self,
        key: str,
        observed: tuple[tuple[str, str], ...],
        *,
        transport_kind: str | None,
    ) -> AuthMigrationFinding | None:
        """Decide whether a declared credential helper can affect the proposed transport.

        An ambient helper only matters if an execution path the migration proposes can actually
        consult it. The pinned SSH transport never reads credential helpers, and the isolated
        HTTPS transport resets the ambient helper chain and installs an operation-scoped reviewed
        helper, so a declaration in those scopes is evidence, not a blocker. Only when no
        transport can be proposed yet does a declared helper remain blocking, because then git
        could still run with the ambient chain intact.
        """

        origins = ", ".join(origin for origin, _value in observed)
        if transport_kind == "ssh":
            return AuthMigrationFinding(
                code=AuthMigrationFindingCode.CREDENTIAL_HELPER_CONFIGURED,
                severity=AuthMigrationSeverity.INFO,
                subject=key,
                detail=(
                    f"{key} is configured in {origins}, but the proposed SSH transport pins an "
                    "identity file and never consults credential helpers, so it cannot supply an "
                    "unreviewed credential."
                ),
            )
        if transport_kind == "https":
            return AuthMigrationFinding(
                code=AuthMigrationFindingCode.CREDENTIAL_HELPER_CONFIGURED,
                severity=AuthMigrationSeverity.INFO,
                subject=key,
                detail=(
                    f"{key} is configured in {origins}, but the proposed isolated HTTPS "
                    "transport resets the ambient helper chain and installs an operation-scoped "
                    "reviewed helper, so it cannot supply an unreviewed credential."
                ),
            )
        return AuthMigrationFinding(
            code=AuthMigrationFindingCode.CREDENTIAL_HELPER_CONFIGURED,
            severity=AuthMigrationSeverity.BLOCKING,
            subject=key,
            detail=(
                f"{key} is configured in {origins}, so Git could supply an unreviewed credential."
            ),
            recovery_actions=(RecoveryAction(RecoveryActionKind.REAUTHORIZE),),
        )

    def _remote_findings(
        self, repo_path: Path, observation: RepositoryIdentityObservation
    ) -> tuple[AuthMigrationFinding, ...]:
        observed = self._ambient.git_config_values(repo_path, _REMOTE_KEY)
        if not observed:
            return ()
        expected_host, expected_owner, expected_repository = observation.canonical_name.split("/")
        mismatched: list[str] = []
        for origin, url in observed:
            match = _REMOTE_TARGET.match(url)
            if match is None or (
                match.group("host").lower() != expected_host
                or match.group("owner").lower() != expected_owner.lower()
                or match.group("repository").lower() != expected_repository.lower()
            ):
                mismatched.append(origin)
        if not mismatched:
            return ()
        return (
            AuthMigrationFinding(
                code=AuthMigrationFindingCode.REMOTE_TARGET_MISMATCH,
                severity=AuthMigrationSeverity.BLOCKING,
                subject=_REMOTE_KEY,
                detail=(
                    f"{_REMOTE_KEY} in "
                    + ", ".join(mismatched)
                    + f" does not point at the observed target {observation.canonical_name}."
                ),
                recovery_actions=(RecoveryAction(RecoveryActionKind.REVIEW_REMOTE),),
            ),
        )

    # -- proposal ------------------------------------------------------------

    def _proposed_changes(
        self,
        repo_id: str,
        observation: RepositoryIdentityObservation,
        account: NamedAccountCandidate,
        ssh: SshAliasCandidate | None,
    ) -> tuple[AuthMigrationChange, ...]:
        profile = self._proposed_profile(observation, account, ssh)
        transport_attributes: list[tuple[str, str]] = [("transport_kind", profile.transport_kind)]
        if profile.ssh_identity_file is not None:
            transport_attributes.append(("ssh_identity_file", profile.ssh_identity_file))
        if profile.https_token_environment is not None:
            transport_attributes.append(
                ("https_token_environment", profile.https_token_environment)
            )
        return (
            AuthMigrationChange(
                kind=AuthMigrationChangeKind.CREATE_PROFILE,
                repo_id=repo_id,
                profile_id=profile.profile_id,
                summary=(
                    f"Declare the locally configured {account.login} account as a reviewed "
                    "auth profile."
                ),
                attributes=(
                    ("github_host", profile.github_host),
                    ("github_login", account.login),
                    ("expected_actor_id", profile.expected_actor_id),
                    ("actor_class", profile.actor_class),
                    ("capability_ids", ",".join(profile.capability_ids)),
                ),
            ),
            AuthMigrationChange(
                kind=AuthMigrationChangeKind.PIN_TRANSPORT,
                repo_id=repo_id,
                profile_id=profile.profile_id,
                summary="Pin the Git transport this profile is allowed to use.",
                attributes=tuple(transport_attributes),
            ),
            AuthMigrationChange(
                kind=AuthMigrationChangeKind.CREATE_BINDING,
                repo_id=repo_id,
                profile_id=profile.profile_id,
                summary=(
                    "Bind the profile to the stable repository identity observed for this "
                    "repository."
                ),
                attributes=(
                    ("repository_id", observation.repository_id),
                    ("canonical_name", observation.canonical_name),
                    ("boundary_id", profile.boundary_id),
                ),
            ),
        )

    def _proposed_profile(
        self,
        observation: RepositoryIdentityObservation,
        account: NamedAccountCandidate,
        ssh: SshAliasCandidate | None,
    ) -> SourceAuthProfile:
        host, owner, _repository = observation.canonical_name.split("/")
        actor_id = account.actor_id or account.login
        if ssh is not None:
            transport_kind = "ssh"
            ssh_identity_file: str | None = ssh.identity_file
            https_token_environment: str | None = None
            fingerprint = _reference_fingerprint("ssh", host, ssh.identity_file)
        else:
            transport_kind = "https"
            ssh_identity_file = None
            https_token_environment = _token_environment(account.login)
            fingerprint = _reference_fingerprint("https", host, account.login)
        return SourceAuthProfile(
            profile_id=account.login,
            provider="github",
            credential_kind="stored_account",
            credential_reference=f"gh-account-{account.login}",
            actor_class="human_operated",
            expected_actor_id=actor_id,
            enabled=True,
            repository_id=observation.repository_id,
            repository_patterns=(f"{host}/{owner}/*",),
            boundary_id=f"{host}-{owner}".replace(".", "-"),
            capability_ids=_MIGRATED_CAPABILITIES,
            github_host=host,
            transport_kind=transport_kind,
            credential_fingerprint=fingerprint,
            allowed_access=("read", "write"),
            github_login=account.login,
            ssh_identity_file=ssh_identity_file,
            https_token_environment=https_token_environment,
            source_ssh_alias=ssh.alias if ssh is not None else None,
        )

    # -- writing -------------------------------------------------------------

    def _write(self, gathered: _Gathered, *, actor: str) -> dict[str, object]:
        assert gathered.verified is not None  # guaranteed by a ready plan with changes
        profile = self._proposed_profile(gathered.observation, gathered.verified, gathered.ssh)
        updated_source = SourceConfiguration(
            gathered.source.tunnel_id,
            gathered.source.profile,
            gathered.source.repositories,
            gathered.source.mcp_connection_max_ttl_seconds,
            tuple(
                sorted(
                    (*gathered.source.auth_profiles, profile),
                    key=lambda item: item.profile_id,
                )
            ),
        )
        source_text = render_source(updated_source)
        document = parse_resolved(self._store.read_resolved_text(gathered.generation))
        document = apply_auth_profiles(document, updated_source.auth_profiles)
        candidate_generation = self._store.next_generation()
        current = self._store.current()
        fingerprints = (
            tuple(sorted(current.repository_fingerprint_map().items())) if current else ()
        )
        resolved_text = render_resolved(
            document,
            generation=candidate_generation,
            source_path=str(self._store.source_path),
            source_sha256=sha256_text(source_text),
            created_at=self._clock.now_iso(),
            reason=f"migrate reviewed auth profile for {gathered.repo_id}",
            proposal_id=None,
            repository_fingerprints=fingerprints,
        )
        now = self._clock.now_iso()
        proposal_id = gathered.plan.plan_id
        generation = self._store.accept(
            ConfigMutation(
                source_text,
                resolved_text,
                fingerprints,
                f"migrate reviewed auth profile for {gathered.repo_id}",
                now,
                gathered.generation,
                gathered.source_sha256,
                proposal_id=proposal_id,
                approval=ApprovalEvent(
                    actor,
                    now,
                    proposal_id,
                    sha256_text(gathered.plan.plan_hash),
                ),
                correlation_id=self._ids.new_hex(24),
            )
        )
        return {
            "status": "applied",
            "generation": generation.generation,
            "profile_id": profile.profile_id,
            "plan_id": gathered.plan.plan_id,
            "safe_next_action": (
                "Ask the operator to run `rf runtime reload` so the reviewed profile is active."
            ),
        }


def _signing_is_active(signer_scopes: list[tuple[str, tuple[tuple[str, str], ...]]]) -> bool:
    """Whether a signing key or an enabled gpgSign makes a signer actually matter.

    `gpg.format` alone only names a format; `commit.gpgsign=false` explicitly disables
    signing, so neither one puts a signer "in force". A configured `user.signingkey` or a
    truthy `commit.gpgsign` does.
    """

    for key, observed in signer_scopes:
        values = [value.strip().lower() for _origin, value in observed]
        if key == "user.signingkey" and any(values):
            return True
        if key == "commit.gpgsign" and any(value in _SIGNING_TRUE for value in values):
            return True
    return False
