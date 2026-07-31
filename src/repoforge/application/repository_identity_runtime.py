"""Effectful repository-identity admission and bounded broker sessions.

The runtime composes existing deterministic authorities. It never creates bindings, exposes
credential material, or performs a publication. An effectful caller must already have an exact
durable binding for the selected actor role before it can open a credential session.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ..config import AppConfig, AuthProfileConfig
from ..domain.auth_profile import AuthProfileSelector, AuthSelectionRequest, resolve_auth_profile
from ..domain.durable_state import Revision
from ..domain.errors import ErrorCode, RepoForgeError
from ..domain.git_transport_identity import GitTransportKind
from ..domain.repository_auth_broker import (
    AuthBrokerRequest,
    AuthBrokerSession,
    RepositoryAuthBroker,
)
from ..domain.repository_identity import (
    AuthTargetKind,
    CredentialProfile,
    RepositoryIdentityBinding,
)
from ..domain.repository_identity_resolution import (
    RepositoryBindingSnapshot,
    RepositoryIdentityObservation,
    RepositoryResolutionOutcome,
)
from ..ports.clock import Clock
from ..ports.repository_binding_store import RepositoryBindingStore

_MAX_BINDINGS = 500


class ObserveRepository(Protocol):
    def __call__(
        self, repo_id: str, selector: AuthProfileSelector
    ) -> RepositoryIdentityObservation: ...


def _error(code: ErrorCode, message: str, *, next_action: str) -> RepoForgeError:
    return RepoForgeError(
        message,
        code=code,
        retryable=False,
        unchanged_state=(
            "No repository binding was created or changed.",
            "No credential session was opened.",
            "No remote effect was attempted.",
        ),
        safe_next_action=next_action,
    )


@dataclass(frozen=True, slots=True)
class RepositoryIdentityAdmission:
    repo_id: str
    observation: RepositoryIdentityObservation
    profile: CredentialProfile
    binding: RepositoryIdentityBinding
    binding_revision: Revision
    selector: AuthProfileSelector

    def __post_init__(self) -> None:
        if not isinstance(self.repo_id, str) or not self.repo_id or len(self.repo_id) > 128:
            raise ValueError("repo_id must be bounded non-empty text")
        if not isinstance(self.observation, RepositoryIdentityObservation):
            raise ValueError("observation must be a RepositoryIdentityObservation")
        if not isinstance(self.profile, CredentialProfile):
            raise ValueError("profile must be a CredentialProfile")
        if not isinstance(self.binding, RepositoryIdentityBinding):
            raise ValueError("binding must be a RepositoryIdentityBinding")
        if not isinstance(self.binding_revision, Revision):
            raise ValueError("binding_revision must be a Revision")
        if not isinstance(self.selector, AuthProfileSelector):
            raise ValueError("selector must be an AuthProfileSelector")


class RepositoryIdentityRuntime:
    """Resolve exact durable identity admission and open bounded credential sessions."""

    def __init__(
        self,
        *,
        config: AppConfig,
        bindings: RepositoryBindingStore,
        broker: RepositoryAuthBroker,
        observe: ObserveRepository,
        clock: Clock,
    ) -> None:
        self._config = config
        self._bindings = bindings
        self._broker = broker
        self._observe = observe
        self._clock = clock

    def _snapshots(self) -> tuple[RepositoryBindingSnapshot, ...]:
        page = self._bindings.list_bindings(max_records=_MAX_BINDINGS)
        return tuple(
            RepositoryBindingSnapshot(binding=item.value, revision=item.revision)
            for item in page.records
        )

    def resolve(
        self, *, repo_id: str, selector: AuthProfileSelector
    ) -> RepositoryIdentityAdmission:
        if repo_id not in self._config.repositories:
            raise _error(
                ErrorCode.NOT_FOUND,
                f"Unknown repository id: {repo_id}",
                next_action="Run repo_list and select one configured repository.",
            )
        observation = self._observe(repo_id, selector)
        resolution = resolve_auth_profile(
            AuthSelectionRequest(
                observation=observation,
                selector=selector,
                bindings=self._snapshots(),
                profiles=tuple(
                    configured.eligibility for configured in self._config.auth_profiles.values()
                ),
            )
        )
        if resolution.outcome is RepositoryResolutionOutcome.PROPOSAL_REQUIRED:
            raise _error(
                ErrorCode.OPERATION_IDENTITY_NOT_FOUND,
                "This effectful operation has no exact durable repository identity binding.",
                next_action=(
                    "Review and create the proposed binding with `rf auth bind` before retrying."
                ),
            )
        if resolution.outcome is RepositoryResolutionOutcome.FAILED:
            assert resolution.failure is not None
            raise _error(
                ErrorCode.CREDENTIAL_SCOPE_MISMATCH,
                resolution.failure.message,
                next_action="Run `rf auth doctor` and select a profile matching the binding role.",
            )
        assert resolution.profile is not None
        assert resolution.binding is not None
        assert resolution.binding_revision is not None
        return RepositoryIdentityAdmission(
            repo_id=repo_id,
            observation=observation,
            profile=resolution.profile,
            binding=resolution.binding,
            binding_revision=resolution.binding_revision,
            selector=selector,
        )

    def _configured(self, admission: RepositoryIdentityAdmission) -> AuthProfileConfig:
        configured = self._config.auth_profiles.get(admission.profile.profile_id)
        if configured is None or configured.profile != admission.profile:
            raise _error(
                ErrorCode.CONFIG_STALE,
                "Repository identity admission no longer matches the active configuration.",
                next_action="Resolve repository identity again against the active configuration.",
            )
        persisted = self._bindings.read(
            admission.binding.provider_host,
            admission.binding.repository_id,
        )
        if (
            persisted is None
            or persisted.revision != admission.binding_revision
            or persisted.value != admission.binding
        ):
            raise _error(
                ErrorCode.STATE_STALE,
                "Repository identity binding changed after admission.",
                next_action="Resolve repository identity again before opening a credential session.",
            )
        return configured

    def session(
        self,
        admission: RepositoryIdentityAdmission,
        *,
        target_kind: AuthTargetKind,
        target_id: str,
        required_capability_ids: tuple[str, ...],
    ) -> AuthBrokerSession:
        if not isinstance(admission, RepositoryIdentityAdmission):
            raise ValueError("admission must be a RepositoryIdentityAdmission")
        configured = self._configured(admission)
        if (
            target_kind is not AuthTargetKind.REPOSITORY
            or target_id != admission.observation.repository_id
        ):
            raise _error(
                ErrorCode.CREDENTIAL_SCOPE_MISMATCH,
                "Credential session target does not match the admitted repository identity.",
                next_action="Use the exact stable repository ID from the current admission.",
            )
        allowed_environment = ["GH_TOKEN"]
        if (
            configured.transport.kind is GitTransportKind.HTTPS
            and configured.transport.https_token_environment is not None
        ):
            allowed_environment.append(configured.transport.https_token_environment)
        return self._broker.session(
            AuthBrokerRequest(
                profile=admission.profile,
                target_kind=target_kind,
                target_id=target_id,
                required_capability_ids=required_capability_ids,
                allowed_environment_keys=tuple(allowed_environment),
                now=self._clock.now_iso(),
            )
        )
