"""Public deterministic auth-profile selection contracts.

Selectors are safe metadata only. Resolution delegates to the stable repository identity
resolver and never observes process-global GitHub, Git, SSH, or credential state.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from .repository_identity_resolution import (
    CredentialProfileEligibility,
    CredentialRole,
    RepositoryBindingSnapshot,
    RepositoryIdentityObservation,
    RepositoryIdentityResolution,
    resolve_repository_identity,
)
from .versioning import Revision

_SAFE_PROFILE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SECRET_SHAPED_PROFILE_ID = re.compile(
    r"^(?:gh[pousr]_|github_pat_|Bearer[: ]|Authorization[: ])",
    re.IGNORECASE,
)


class RequestedActorClass(str, Enum):
    """Public actor classes accepted by CLI and MCP selectors."""

    HUMAN = "human"
    AGENT = "agent"

    @property
    def role(self) -> CredentialRole:
        """The internal credential role this public actor class selects for."""

        return CredentialRole.AGENT if self is RequestedActorClass.AGENT else CredentialRole.HUMAN


@dataclass(frozen=True, slots=True)
class AuthProfileSelector:
    """Safe public selector with backward-compatible deterministic defaults."""

    auth_profile: str = "auto"
    actor_class: RequestedActorClass = RequestedActorClass.HUMAN

    def __post_init__(self) -> None:
        if (
            not isinstance(self.auth_profile, str)
            or _SAFE_PROFILE_ID.fullmatch(self.auth_profile) is None
            or _SECRET_SHAPED_PROFILE_ID.search(self.auth_profile) is not None
        ):
            raise ValueError("auth_profile must be 'auto' or a safe non-secret profile identifier")
        if not isinstance(self.actor_class, RequestedActorClass):
            raise ValueError("actor_class must be a RequestedActorClass")

    @property
    def role(self) -> CredentialRole:
        return self.actor_class.role

    @property
    def automatic(self) -> bool:
        return self.auth_profile == "auto"

    def payload(self) -> dict[str, str]:
        return {
            "auth_profile": self.auth_profile,
            "actor_class": self.actor_class.value,
        }


@dataclass(frozen=True, slots=True)
class AuthSelectionRequest:
    """All immutable evidence required to resolve one public selector."""

    observation: RepositoryIdentityObservation
    selector: AuthProfileSelector
    bindings: tuple[RepositoryBindingSnapshot, ...]
    profiles: tuple[CredentialProfileEligibility, ...]
    expected_binding_revision: Revision | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.observation, RepositoryIdentityObservation):
            raise ValueError("observation must be a RepositoryIdentityObservation")
        if not isinstance(self.selector, AuthProfileSelector):
            raise ValueError("selector must be an AuthProfileSelector")
        if not isinstance(self.bindings, tuple) or any(
            not isinstance(item, RepositoryBindingSnapshot) for item in self.bindings
        ):
            raise ValueError("bindings must contain RepositoryBindingSnapshot values")
        if not isinstance(self.profiles, tuple) or any(
            not isinstance(item, CredentialProfileEligibility) for item in self.profiles
        ):
            raise ValueError("profiles must contain CredentialProfileEligibility values")
        if self.expected_binding_revision is not None and not isinstance(
            self.expected_binding_revision, Revision
        ):
            raise ValueError("expected_binding_revision must be a Revision or None")


def resolve_auth_profile(request: AuthSelectionRequest) -> RepositoryIdentityResolution:
    """Resolve ``auto`` or one explicit profile without ambient account fallback."""

    if not isinstance(request, AuthSelectionRequest):
        raise ValueError("request must be an AuthSelectionRequest")
    return resolve_repository_identity(
        observation=request.observation,
        role=request.selector.role,
        bindings=request.bindings,
        profiles=request.profiles,
        expected_binding_revision=request.expected_binding_revision,
        selected_profile_id=(None if request.selector.automatic else request.selector.auth_profile),
    )
