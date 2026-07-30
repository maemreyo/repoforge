"""One facade for inspecting, binding, and diagnosing repository identities.

This service coordinates authorities that already exist -- reviewed configuration, the binding
store, the deterministic resolver, the operation identity manager, and the narrow per-surface
inspectors -- and publishes nothing itself.

The seven identity surfaces stay independent on purpose. A reachable transport is not proof of
an API actor; an unsigned commit attestation never names a signer; a missing inspector reports
``unavailable`` instead of quietly falling back to whatever account happens to be active.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol

from ..config import AppConfig, AuthProfileConfig
from ..domain.auth_profile import AuthProfileSelector, AuthSelectionRequest, resolve_auth_profile
from ..domain.commit_identity import CommitSigningMode
from ..domain.durable_state import Revision, StateEnvelope
from ..domain.errors import ErrorCode, RepoForgeError
from ..domain.git_transport_identity import GitTransportKind
from ..domain.repository_identity import (
    RecoveryAction,
    RecoveryActionKind,
    RepositoryIdentityBinding,
)
from ..domain.repository_identity_resolution import (
    CredentialProfileEligibility,
    CredentialRole,
    RepositoryBindingSnapshot,
    RepositoryIdentityObservation,
    RepositoryResolutionOutcome,
    role_accepts_actor_class,
)
from ..ports.auth_inspection import (
    ApiIdentityInspector,
    CommitIdentityInspector,
    PublicationTargetInspector,
    TransportInspector,
)
from ..ports.clock import Clock
from ..ports.repository_binding_store import RepositoryBindingStore
from .operations.identity import OperationIdentityManager

_MAX_BINDINGS = 500


class ObserveRepository(Protocol):
    def __call__(self, repo_id: str) -> RepositoryIdentityObservation: ...


class AuthSurface(str, Enum):
    """The independent surfaces, in the stable order every report uses."""

    REPOSITORY_BINDING = "repository_binding"
    API = "api"
    TRANSPORT = "transport"
    COMMIT_AUTHOR = "commit_author"
    COMMIT_COMMITTER = "commit_committer"
    COMMIT_SIGNER = "commit_signer"
    PUBLICATION = "publication"


#: Iteration order for every whoami report, so callers can diff two runs line by line.
AUTH_SURFACE_ORDER: tuple[AuthSurface, ...] = tuple(AuthSurface)


class AuthSurfaceState(str, Enum):
    #: Observed live against the selected identity.
    VERIFIED = "verified"
    #: Declared in reviewed configuration but not observed on this call.
    CONFIGURED = "configured"
    #: The surface cannot expose the fact being asked for, by design.
    UNOBSERVABLE = "unobservable"
    #: Observed and denied.
    BLOCKED = "blocked"
    #: No reviewed configuration or no production inspector for this surface.
    UNAVAILABLE = "unavailable"


class AuthDoctorSeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    BLOCKING = "blocking"


@dataclass(frozen=True, slots=True)
class AuthSurfaceEvidence:
    surface: AuthSurface
    state: AuthSurfaceState
    detail: str
    profile_id: str | None = None
    actor_id: str | None = None
    required: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.surface, AuthSurface):
            raise ValueError("surface must be an AuthSurface")
        if not isinstance(self.state, AuthSurfaceState):
            raise ValueError("state must be an AuthSurfaceState")
        if not isinstance(self.detail, str) or not self.detail or len(self.detail) > 1_000:
            raise ValueError("detail must be bounded text")
        if not isinstance(self.required, bool):
            raise ValueError("required must be boolean")

    @property
    def satisfied(self) -> bool:
        """Whether this surface can carry a durable write right now."""

        return self.state in {AuthSurfaceState.VERIFIED, AuthSurfaceState.UNOBSERVABLE}

    def safe_payload(self) -> dict[str, object]:
        return {
            "surface": self.surface.value,
            "state": self.state.value,
            "detail": self.detail,
            "profile_id": self.profile_id,
            "actor_id": self.actor_id,
            "required": self.required,
        }


@dataclass(frozen=True, slots=True)
class AuthWhoamiResult:
    repo_id: str
    profile_id: str | None
    surfaces: tuple[AuthSurfaceEvidence, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.surfaces, tuple) or any(
            not isinstance(item, AuthSurfaceEvidence) for item in self.surfaces
        ):
            raise ValueError("surfaces must contain AuthSurfaceEvidence values")
        ordered = [item.surface for item in self.surfaces]
        if ordered != [surface for surface in AUTH_SURFACE_ORDER if surface in set(ordered)]:
            raise ValueError("surfaces must follow the stable surface order")

    @property
    def ready(self) -> bool:
        """Readiness depends on every *requested required* surface, not on any single one."""

        required = [item for item in self.surfaces if item.required]
        return bool(required) and all(item.satisfied for item in required)

    def safe_payload(self) -> dict[str, object]:
        return {
            "repo_id": self.repo_id,
            "profile_id": self.profile_id,
            "ready": self.ready,
            "surfaces": [item.safe_payload() for item in self.surfaces],
        }


@dataclass(frozen=True, slots=True)
class AuthDoctorFinding:
    code: str
    severity: AuthDoctorSeverity
    subject: str
    detail: str
    recovery_actions: tuple[RecoveryAction, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.severity, AuthDoctorSeverity):
            raise ValueError("severity must be an AuthDoctorSeverity")
        for value, field in ((self.code, "code"), (self.subject, "subject")):
            if not isinstance(value, str) or not value or len(value) > 300:
                raise ValueError(f"{field} must be bounded text")
        if not isinstance(self.detail, str) or not self.detail or len(self.detail) > 1_000:
            raise ValueError("detail must be bounded text")

    def safe_payload(self) -> dict[str, object]:
        return {
            "code": self.code,
            "severity": self.severity.value,
            "subject": self.subject,
            "detail": self.detail,
            "recovery_actions": [item.payload() for item in self.recovery_actions],
        }


def _error(code: ErrorCode, message: str, *, next_action: str) -> RepoForgeError:
    return RepoForgeError(
        message,
        code=code,
        retryable=False,
        unchanged_state=("No repository binding, lease, or external write was changed.",),
        safe_next_action=next_action,
    )


class AuthUxService:
    """Read and change reviewed identity state; never acquire material or publish."""

    def __init__(
        self,
        *,
        config: AppConfig,
        bindings: RepositoryBindingStore,
        observe: ObserveRepository,
        identities: OperationIdentityManager | None = None,
        clock: Clock | None = None,
        api: ApiIdentityInspector | None = None,
        transport: TransportInspector | None = None,
        commits: CommitIdentityInspector | None = None,
        publication: PublicationTargetInspector | None = None,
    ) -> None:
        self._config = config
        self._bindings = bindings
        self._observe = observe
        self._identities = identities
        self._clock = clock
        self._api = api
        self._transport = transport
        self._commits = commits
        self._publication = publication

    # -- profiles ------------------------------------------------------------

    def profile_list(
        self, *, enabled_only: bool = False, role: CredentialRole | None = None
    ) -> list[dict[str, object]]:
        result: list[dict[str, object]] = []
        for profile_id in sorted(self._config.auth_profiles):
            configured = self._config.auth_profiles[profile_id]
            if enabled_only and not configured.eligibility.enabled:
                continue
            if role is not None and not _role_matches(configured, role):
                continue
            result.append(self._profile_payload(configured))
        return result

    def profile_inspect(self, profile_id: str) -> dict[str, object]:
        configured = self._config.auth_profiles.get(profile_id)
        if configured is None:
            raise _error(
                ErrorCode.NOT_FOUND,
                f"No auth profile is declared with id {profile_id!r}.",
                next_action="Run `rf auth profile list` to see the declared profiles.",
            )
        return self._profile_payload(configured)

    @staticmethod
    def _profile_payload(configured: AuthProfileConfig) -> dict[str, object]:
        profile = configured.profile
        return {
            "profile_id": profile.profile_id,
            "provider": profile.provider.value,
            "credential_kind": profile.credential_kind.value,
            "actor_class": profile.actor_class.value,
            "expected_actor_id": profile.expected_actor_id,
            "enabled": configured.eligibility.enabled,
            "repository_patterns": list(configured.eligibility.repository_patterns),
            "boundary_id": configured.eligibility.boundary_id,
            "capability_ids": list(profile.capability_ids),
            "transport_kind": configured.transport.kind.value,
            "source_ssh_alias": configured.source_ssh_alias,
        }

    # -- resolution and bindings ---------------------------------------------

    def resolve(
        self,
        *,
        repo_id: str,
        selector: AuthProfileSelector,
        expected_binding_revision: int | None = None,
    ) -> dict[str, object]:
        observation = self._observe(repo_id)
        resolution = resolve_auth_profile(
            AuthSelectionRequest(
                observation=observation,
                selector=selector,
                bindings=self._binding_snapshots(),
                profiles=tuple(item.eligibility for item in self._config.auth_profiles.values()),
                expected_binding_revision=(
                    Revision(expected_binding_revision)
                    if expected_binding_revision is not None
                    else None
                ),
            )
        )
        return {"repo_id": repo_id, "selector": selector.payload(), **resolution.payload()}

    def bind(
        self,
        *,
        repo_id: str,
        selector: AuthProfileSelector,
        expected_binding_revision: int | None = None,
    ) -> dict[str, object]:
        """Persist the binding the resolver proposes, or confirm the existing one unchanged.

        Repeating a bind is idempotent: an exact binding that already satisfies the requested
        role resolves and is reported unchanged rather than rewritten.
        """

        observation = self._observe(repo_id)
        expected = (
            Revision(expected_binding_revision) if expected_binding_revision is not None else None
        )
        eligibility = tuple(item.eligibility for item in self._config.auth_profiles.values())
        resolution = resolve_auth_profile(
            AuthSelectionRequest(
                observation=observation,
                selector=selector,
                bindings=self._binding_snapshots(),
                profiles=eligibility,
                expected_binding_revision=expected,
            )
        )
        existing = self._bindings.read(observation.provider_host, observation.repository_id)
        if resolution.outcome is RepositoryResolutionOutcome.RESOLVED:
            assert resolution.binding is not None
            return {
                "status": "unchanged",
                "repo_id": repo_id,
                "binding": resolution.binding.payload(),
                "revision": existing.revision.value if existing is not None else None,
            }
        if resolution.outcome is RepositoryResolutionOutcome.PROPOSAL_REQUIRED:
            proposal = resolution.proposal
            assert proposal is not None
            created = self._bindings.create(proposal.binding)
            return {
                "status": "created",
                "repo_id": repo_id,
                "binding": created.value.payload(),
                "revision": created.revision.value,
            }
        # A binding that exists but has no profile for the requested role is not an error to
        # resolve against -- it is exactly the "add the other role" case, and filling that one
        # empty slot is the only change allowed here.
        if existing is not None and self._role_slot(existing.value, selector.role) is None:
            return self._fill_role_slot(
                repo_id,
                existing,
                observation,
                selector,
                eligibility,
                expected=expected,
            )
        failure = resolution.failure
        assert failure is not None
        raise _error(
            ErrorCode.CREDENTIAL_SCOPE_MISMATCH,
            failure.message,
            next_action="Run `rf auth doctor` to see the typed recovery actions.",
        )

    @staticmethod
    def _role_slot(binding: RepositoryIdentityBinding, role: CredentialRole) -> str | None:
        return (
            binding.agent_profile_id if role is CredentialRole.AGENT else binding.human_profile_id
        )

    def _fill_role_slot(
        self,
        repo_id: str,
        existing: StateEnvelope[RepositoryIdentityBinding],
        observation: RepositoryIdentityObservation,
        selector: AuthProfileSelector,
        eligibility: tuple[CredentialProfileEligibility, ...],
        *,
        expected: Revision | None,
    ) -> dict[str, object]:
        if expected is not None and existing.revision != expected:
            raise _error(
                ErrorCode.STATE_STALE,
                "The repository binding revision changed after it was read.",
                next_action="Re-read the binding with `rf auth resolve` and retry.",
            )
        # Selecting the profile for the empty slot runs the same eligibility rules as a fresh
        # binding: exactly one enabled, matching, role-compatible profile, or nothing.
        candidates = tuple(
            item
            for item in eligibility
            if item.enabled
            and item.matches(observation)
            and _role_matches_eligibility(item, selector.role)
            and (selector.automatic or item.profile.profile_id == selector.auth_profile)
        )
        if len(candidates) != 1:
            raise _error(
                ErrorCode.CREDENTIAL_SCOPE_MISMATCH,
                (
                    "No single eligible profile exists for this actor role, so the empty role "
                    "slot cannot be filled deterministically."
                ),
                next_action="Declare the intended profile explicitly with `--auth-profile`.",
            )
        profile_id = candidates[0].profile.profile_id
        binding = existing.value
        updated = RepositoryIdentityBinding(
            provider=binding.provider,
            repository_id=binding.repository_id,
            canonical_name=binding.canonical_name,
            human_profile_id=(
                profile_id if selector.role is CredentialRole.HUMAN else binding.human_profile_id
            ),
            agent_profile_id=(
                profile_id if selector.role is CredentialRole.AGENT else binding.agent_profile_id
            ),
            config_revision=binding.config_revision,
            provider_host=binding.provider_host,
        )
        saved = self._bindings.save(updated, expected_revision=existing.revision)
        return {
            "status": "updated",
            "repo_id": repo_id,
            "binding": saved.value.payload(),
            "revision": saved.revision.value,
        }

    def unbind(
        self,
        *,
        repo_id: str,
        role: CredentialRole,
        expected_binding_revision: int,
    ) -> dict[str, object]:
        """Clear exactly one role slot.

        Clearing the only remaining role is refused: a binding with no profile is not a
        representable state, and dropping the whole binding is a different, reviewed decision
        than narrowing which actor classes may use it.
        """

        observation = self._observe(repo_id)
        existing = self._bindings.read(observation.provider_host, observation.repository_id)
        if existing is None:
            raise _error(
                ErrorCode.NOT_FOUND,
                "This repository has no binding to unbind.",
                next_action="Run `rf auth resolve` to see the current identity state.",
            )
        if existing.revision.value != expected_binding_revision:
            raise _error(
                ErrorCode.STATE_STALE,
                "The repository binding revision changed after it was read.",
                next_action="Re-read the binding with `rf auth resolve` and retry.",
            )
        binding = existing.value
        human = None if role is CredentialRole.HUMAN else binding.human_profile_id
        agent = None if role is CredentialRole.AGENT else binding.agent_profile_id
        if human is None and agent is None:
            raise _error(
                ErrorCode.INPUT_REQUIRED,
                "This is the final role on the binding, so clearing it would leave no identity.",
                next_action=(
                    "Bind the other actor role first, or remove the repository from reviewed "
                    "configuration if it should no longer be reachable."
                ),
            )
        updated = RepositoryIdentityBinding(
            provider=binding.provider,
            repository_id=binding.repository_id,
            canonical_name=binding.canonical_name,
            human_profile_id=human,
            agent_profile_id=agent,
            config_revision=binding.config_revision,
            provider_host=binding.provider_host,
        )
        saved = self._bindings.save(updated, expected_revision=existing.revision)
        return {
            "status": "updated",
            "repo_id": repo_id,
            "binding": saved.value.payload(),
            "revision": saved.revision.value,
        }

    def _binding_snapshots(self) -> tuple[RepositoryBindingSnapshot, ...]:
        page = self._bindings.list_bindings(max_records=_MAX_BINDINGS)
        return tuple(
            RepositoryBindingSnapshot(binding=item.value, revision=item.revision)
            for item in page.records
        )

    # -- surfaces ------------------------------------------------------------

    def whoami(
        self,
        *,
        repo_id: str,
        checks: tuple[AuthSurface, ...] | None = None,
    ) -> AuthWhoamiResult:
        requested = (
            AUTH_SURFACE_ORDER
            if checks is None
            else tuple(surface for surface in AUTH_SURFACE_ORDER if surface in set(checks))
        )
        observation = self._observe(repo_id)
        snapshots = self._binding_snapshots()
        exact = tuple(
            item
            for item in snapshots
            if item.binding.provider_host == observation.provider_host
            and item.binding.repository_id == observation.repository_id
        )
        profile_id = (
            exact[0].binding.human_profile_id or exact[0].binding.agent_profile_id
            if exact
            else None
        )
        configured = self._config.auth_profiles.get(profile_id) if profile_id else None
        path = self._repository_path(repo_id)

        builders = {
            AuthSurface.REPOSITORY_BINDING: lambda: self._binding_surface(exact, observation),
            AuthSurface.API: lambda: self._api_surface(configured),
            AuthSurface.TRANSPORT: lambda: self._transport_surface(configured, path),
            AuthSurface.COMMIT_AUTHOR: lambda: self._commit_surface(
                AuthSurface.COMMIT_AUTHOR, path
            ),
            AuthSurface.COMMIT_COMMITTER: lambda: self._commit_surface(
                AuthSurface.COMMIT_COMMITTER, path
            ),
            AuthSurface.COMMIT_SIGNER: lambda: self._signer_surface(path),
            AuthSurface.PUBLICATION: lambda: self._publication_surface(path, observation),
        }
        return AuthWhoamiResult(
            repo_id=repo_id,
            profile_id=profile_id,
            surfaces=tuple(builders[surface]() for surface in requested),
        )

    def _repository_path(self, repo_id: str) -> Path | None:
        repository = self._config.repositories.get(repo_id)
        return repository.path if repository is not None else None

    @staticmethod
    def _binding_surface(
        exact: tuple[RepositoryBindingSnapshot, ...],
        observation: RepositoryIdentityObservation,
    ) -> AuthSurfaceEvidence:
        if not exact:
            return AuthSurfaceEvidence(
                surface=AuthSurface.REPOSITORY_BINDING,
                state=AuthSurfaceState.UNAVAILABLE,
                detail="No binding claims this stable repository identity yet.",
            )
        binding = exact[0].binding
        if binding.config_revision != observation.config_revision:
            return AuthSurfaceEvidence(
                surface=AuthSurface.REPOSITORY_BINDING,
                state=AuthSurfaceState.BLOCKED,
                detail="The binding belongs to a different configuration revision.",
                profile_id=binding.human_profile_id or binding.agent_profile_id,
            )
        return AuthSurfaceEvidence(
            surface=AuthSurface.REPOSITORY_BINDING,
            state=AuthSurfaceState.VERIFIED,
            detail=f"Bound to {binding.canonical_name} ({binding.repository_id}).",
            profile_id=binding.human_profile_id or binding.agent_profile_id,
            actor_id=binding.repository_id,
        )

    def _api_surface(self, configured: AuthProfileConfig | None) -> AuthSurfaceEvidence:
        if configured is None:
            return AuthSurfaceEvidence(
                surface=AuthSurface.API,
                state=AuthSurfaceState.UNAVAILABLE,
                detail="No reviewed auth profile is selected for this repository.",
            )
        if self._api is None:
            # The identity is declared, it simply was not proved on this call. That is a
            # different answer from "nothing is declared", and a different next step.
            return AuthSurfaceEvidence(
                surface=AuthSurface.API,
                state=AuthSurfaceState.CONFIGURED,
                detail=(
                    f"{configured.profile.expected_actor_id} is the reviewed API actor. No API "
                    "identity inspector is composed here, so it was not proved live."
                ),
                profile_id=configured.profile.profile_id,
            )
        try:
            proof = self._api.inspect(configured.api_identity)
        except RepoForgeError as exc:
            return AuthSurfaceEvidence(
                surface=AuthSurface.API,
                state=AuthSurfaceState.BLOCKED,
                detail=f"The API actor probe was denied: {exc.code.value}.",
                profile_id=configured.profile.profile_id,
            )
        if proof.revoked or not proof.approved or not proof.sso_authorized:
            return AuthSurfaceEvidence(
                surface=AuthSurface.API,
                state=AuthSurfaceState.BLOCKED,
                detail="The observed API identity is revoked or not authorized.",
                profile_id=configured.profile.profile_id,
                actor_id=proof.actor_id,
            )
        if proof.actor_id != configured.profile.expected_actor_id:
            return AuthSurfaceEvidence(
                surface=AuthSurface.API,
                state=AuthSurfaceState.BLOCKED,
                detail="The observed API actor does not match the reviewed profile.",
                profile_id=configured.profile.profile_id,
                actor_id=proof.actor_id,
            )
        return AuthSurfaceEvidence(
            surface=AuthSurface.API,
            state=AuthSurfaceState.VERIFIED,
            detail="The API actor matches the reviewed profile.",
            profile_id=configured.profile.profile_id,
            actor_id=proof.actor_id,
        )

    def _transport_surface(
        self, configured: AuthProfileConfig | None, path: Path | None
    ) -> AuthSurfaceEvidence:
        if configured is None:
            return AuthSurfaceEvidence(
                surface=AuthSurface.TRANSPORT,
                state=AuthSurfaceState.UNAVAILABLE,
                detail="No reviewed auth profile is selected for this repository.",
            )
        if self._transport is None or path is None:
            return AuthSurfaceEvidence(
                surface=AuthSurface.TRANSPORT,
                state=AuthSurfaceState.CONFIGURED,
                detail=(
                    f"A pinned {configured.transport.kind.value.upper()} transport is reviewed "
                    "for this repository. No transport inspector is composed here, so it was "
                    "not exercised."
                ),
                profile_id=configured.profile.profile_id,
            )
        try:
            evidence = self._transport.inspect(path, configured.transport)
        except RepoForgeError as exc:
            return AuthSurfaceEvidence(
                surface=AuthSurface.TRANSPORT,
                state=AuthSurfaceState.BLOCKED,
                detail=f"The pinned transport could not authenticate: {exc.code.value}.",
                profile_id=configured.profile.profile_id,
            )
        if evidence.credential_fingerprint != configured.transport.credential_fingerprint:
            return AuthSurfaceEvidence(
                surface=AuthSurface.TRANSPORT,
                state=AuthSurfaceState.BLOCKED,
                detail="The transport used a credential other than the pinned one.",
                profile_id=configured.profile.profile_id,
            )
        kind = "SSH" if configured.transport.kind is GitTransportKind.SSH else "HTTPS"
        # A reachable transport proves reachability only; it can never name an API actor.
        return AuthSurfaceEvidence(
            surface=AuthSurface.TRANSPORT,
            state=AuthSurfaceState.VERIFIED,
            detail=(
                f"The pinned {kind} transport reached {evidence.provider_host}. "
                "Transport evidence never identifies an actor."
            ),
            profile_id=configured.profile.profile_id,
            actor_id=None,
        )

    def _commit_surface(self, surface: AuthSurface, path: Path | None) -> AuthSurfaceEvidence:
        if self._commits is None or path is None:
            return AuthSurfaceEvidence(
                surface=surface,
                state=AuthSurfaceState.UNAVAILABLE,
                detail="No commit identity inspector is composed for this repository.",
            )
        try:
            evidence = self._commits.inspect(path)
        except RepoForgeError as exc:
            return AuthSurfaceEvidence(
                surface=surface,
                state=AuthSurfaceState.BLOCKED,
                detail=f"The pinned commit identity is unusable: {exc.code.value}.",
            )
        if surface is AuthSurface.COMMIT_AUTHOR:
            name, email = evidence.author_name, evidence.author_email
        else:
            name, email = evidence.committer_name, evidence.committer_email
        return AuthSurfaceEvidence(
            surface=surface,
            state=AuthSurfaceState.VERIFIED,
            detail=f"{name} <{email}> is pinned for this worktree.",
            profile_id=evidence.profile_id,
            actor_id=evidence.represented_actor_id,
        )

    def _signer_surface(self, path: Path | None) -> AuthSurfaceEvidence:
        if self._commits is None or path is None:
            return AuthSurfaceEvidence(
                surface=AuthSurface.COMMIT_SIGNER,
                state=AuthSurfaceState.UNAVAILABLE,
                detail="No commit identity inspector is composed for this repository.",
            )
        try:
            evidence = self._commits.inspect(path)
        except RepoForgeError as exc:
            return AuthSurfaceEvidence(
                surface=AuthSurface.COMMIT_SIGNER,
                state=AuthSurfaceState.BLOCKED,
                detail=f"The pinned signing identity is unusable: {exc.code.value}.",
            )
        if evidence.signing_mode is CommitSigningMode.UNSIGNED_ATTESTED:
            # An unsigned attestation is not a claim about a signer; say so rather than
            # reporting a signer this repository does not actually have.
            return AuthSurfaceEvidence(
                surface=AuthSurface.COMMIT_SIGNER,
                state=AuthSurfaceState.UNOBSERVABLE,
                detail="Commits are unsigned, so no signer identity is claimed.",
                profile_id=evidence.profile_id,
            )
        if evidence.signer_fingerprint is None:
            return AuthSurfaceEvidence(
                surface=AuthSurface.COMMIT_SIGNER,
                state=AuthSurfaceState.BLOCKED,
                detail="Signing is required but no signer fingerprint was observed.",
                profile_id=evidence.profile_id,
            )
        return AuthSurfaceEvidence(
            surface=AuthSurface.COMMIT_SIGNER,
            state=AuthSurfaceState.VERIFIED,
            detail=f"Signed with {evidence.signer_fingerprint}.",
            profile_id=evidence.profile_id,
        )

    def _publication_surface(
        self, path: Path | None, observation: RepositoryIdentityObservation
    ) -> AuthSurfaceEvidence:
        if self._publication is None or path is None:
            return AuthSurfaceEvidence(
                surface=AuthSurface.PUBLICATION,
                state=AuthSurfaceState.UNAVAILABLE,
                detail="No publication target inspector is composed for this repository.",
            )
        try:
            topology = self._publication.inspect(path, observation.repository_id)
        except RepoForgeError as exc:
            return AuthSurfaceEvidence(
                surface=AuthSurface.PUBLICATION,
                state=AuthSurfaceState.BLOCKED,
                detail=f"The publication target was denied: {exc.code.value}.",
            )
        return AuthSurfaceEvidence(
            surface=AuthSurface.PUBLICATION,
            state=AuthSurfaceState.VERIFIED,
            detail=(
                f"{topology.remote_name} publishes {topology.source_ref} to "
                f"{topology.destination_ref}."
            ),
            actor_id=observation.repository_id,
        )

    # -- doctor --------------------------------------------------------------

    def doctor(self, *, repo_id: str) -> list[AuthDoctorFinding]:
        """Report every reason this repository could not write, with typed recovery."""

        findings: list[AuthDoctorFinding] = []
        if not self._config.auth_profiles:
            findings.append(
                AuthDoctorFinding(
                    code="migration_required",
                    severity=AuthDoctorSeverity.BLOCKING,
                    subject=repo_id,
                    detail=(
                        "No auth profiles are declared, so no identity can be selected "
                        "deterministically."
                    ),
                    recovery_actions=(RecoveryAction(RecoveryActionKind.RESELECT_PROFILE),),
                )
            )
        for profile_id in sorted(self._config.auth_profiles):
            configured = self._config.auth_profiles[profile_id]
            if not configured.eligibility.enabled:
                findings.append(
                    AuthDoctorFinding(
                        code="profile_disabled",
                        severity=AuthDoctorSeverity.INFO,
                        subject=profile_id,
                        detail="This profile is declared but disabled, so it is never selected.",
                        recovery_actions=(RecoveryAction(RecoveryActionKind.REAUTHORIZE),),
                    )
                )
        resolution = self.resolve(repo_id=repo_id, selector=AuthProfileSelector())
        failure = resolution.get("failure")
        if isinstance(failure, dict):
            actions = tuple(
                RecoveryAction(RecoveryActionKind(str(item["kind"])))
                for item in failure.get("recovery_actions", [])
                if isinstance(item, dict) and "kind" in item
            )
            findings.append(
                AuthDoctorFinding(
                    code=str(failure["code"]).lower(),
                    severity=AuthDoctorSeverity.BLOCKING,
                    subject=repo_id,
                    detail=str(failure["message"]),
                    recovery_actions=actions,
                )
            )
        for evidence in self.whoami(repo_id=repo_id).surfaces:
            if evidence.state is AuthSurfaceState.BLOCKED:
                findings.append(
                    AuthDoctorFinding(
                        code=f"{evidence.surface.value}_blocked",
                        severity=AuthDoctorSeverity.BLOCKING,
                        subject=evidence.surface.value,
                        detail=evidence.detail,
                        recovery_actions=(RecoveryAction(RecoveryActionKind.REVERIFY_ACTOR),),
                    )
                )
            elif evidence.state is AuthSurfaceState.UNAVAILABLE and evidence.required:
                findings.append(
                    AuthDoctorFinding(
                        code=f"{evidence.surface.value}_unavailable",
                        severity=AuthDoctorSeverity.WARNING,
                        subject=evidence.surface.value,
                        detail=evidence.detail,
                        recovery_actions=(RecoveryAction(RecoveryActionKind.RESELECT_PROFILE),),
                    )
                )
        return findings

    # -- leases --------------------------------------------------------------

    def lease_inspect(self, *, operation_id: str) -> dict[str, object]:
        envelope = self._require_identities().inspect(operation_id)
        return {
            "operation_id": operation_id,
            "revision": envelope.revision.value,
            "identity": envelope.value.safe_payload(),
        }

    def lease_revoke(
        self,
        *,
        operation_id: str,
        expected_revision: int,
        lease_id: str | None = None,
        profile_id: str | None = None,
    ) -> dict[str, object]:
        manager = self._require_identities()
        updated = manager.revoke(
            operation_id,
            expected_revision=Revision(expected_revision),
            now=self._now(),
            lease_id=lease_id,
            profile_id=profile_id,
        )
        return {
            "status": "revoked",
            "operation_id": operation_id,
            "identity": updated.safe_payload(),
        }

    def _now(self) -> str:
        if self._clock is None:
            raise _error(
                ErrorCode.OPERATION_IDENTITY_NOT_FOUND,
                "No clock is composed, so a lease lifecycle change cannot be timestamped.",
                next_action="Start the managed runtime before changing operation leases.",
            )
        return self._clock.now_iso()

    def _require_identities(self) -> OperationIdentityManager:
        if self._identities is None:
            raise _error(
                ErrorCode.OPERATION_IDENTITY_NOT_FOUND,
                "Durable operation identity state is not composed in this context.",
                next_action="Start the managed runtime before inspecting operation leases.",
            )
        return self._identities


def _role_matches(configured: AuthProfileConfig, role: CredentialRole) -> bool:
    return role_accepts_actor_class(role, configured.profile.actor_class)


def _role_matches_eligibility(
    eligibility: CredentialProfileEligibility, role: CredentialRole
) -> bool:
    return role_accepts_actor_class(role, eligibility.profile.actor_class)
