"""Deterministic repository identity binding resolution contracts.

Repository names and patterns are discovery hints only. Resolution is anchored to the
provider host plus stable repository ID, and never consults process-global account state.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from .repository_identity import (
    ActorClass,
    CredentialProfile,
    RecoveryAction,
    RecoveryActionKind,
    RepositoryAuthFailure,
    RepositoryAuthFailureCode,
    RepositoryIdentityBinding,
    RepositoryProvider,
)
from .versioning import Revision

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_HOST = re.compile(r"^[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?$")
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_NAME_PART = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")


class CredentialRole(str, Enum):
    HUMAN = "human"
    AGENT = "agent"


class RepositoryResolutionOutcome(str, Enum):
    RESOLVED = "resolved"
    PROPOSAL_REQUIRED = "proposal_required"
    FAILED = "failed"


class RepositoryReconciliationKind(str, Enum):
    RENAMED = "renamed"


def _safe_id(value: str, field: str) -> str:
    if not isinstance(value, str) or _SAFE_ID.fullmatch(value) is None:
        raise ValueError(f"{field} must be a bounded safe identifier")
    return value


def _host(value: str) -> str:
    if not isinstance(value, str) or _HOST.fullmatch(value) is None:
        raise ValueError("provider_host must be a bounded lowercase host")
    return value


def _sha256(value: str, field: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{field} must be a full lowercase SHA-256")
    return value


def _timestamp(value: str, field: str) -> None:
    if not isinstance(value, str) or not value or len(value) > 80:
        raise ValueError(f"{field} must be a bounded timezone-aware timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must include a timezone offset")


def _canonical_parts(value: str, *, expected_host: str | None = None) -> tuple[str, str, str]:
    if not isinstance(value, str) or len(value) > 304:
        raise ValueError("canonical_name must be host/owner/repository")
    parts = value.split("/")
    if (
        len(parts) != 3
        or _HOST.fullmatch(parts[0]) is None
        or _NAME_PART.fullmatch(parts[1]) is None
        or _NAME_PART.fullmatch(parts[2]) is None
    ):
        raise ValueError("canonical_name must be host/owner/repository")
    if expected_host is not None and parts[0] != expected_host:
        raise ValueError("canonical_name host must match provider_host")
    return parts[0], parts[1], parts[2]


def _owner_boundary(canonical_name: str) -> str:
    host, owner, _repository = _canonical_parts(canonical_name)
    return f"{host}/{owner}".lower()


def _pattern_boundary(pattern: str) -> str:
    if not isinstance(pattern, str) or not pattern or len(pattern) > 304:
        raise ValueError("repository pattern must remain inside one owner boundary")
    if pattern.endswith("/*"):
        prefix = pattern[:-2]
        parts = prefix.split("/")
        if (
            len(parts) != 2
            or _HOST.fullmatch(parts[0]) is None
            or _NAME_PART.fullmatch(parts[1]) is None
            or "*" in prefix
        ):
            raise ValueError("repository wildcard must remain inside one owner boundary")
        return prefix.lower()
    if "*" in pattern:
        raise ValueError("repository wildcard must remain inside one owner boundary")
    host, owner, _repository = _canonical_parts(pattern)
    return f"{host}/{owner}".lower()


def _pattern_matches(pattern: str, canonical_name: str) -> bool:
    lowered = canonical_name.lower()
    return (
        lowered.startswith(pattern[:-1].lower())
        if pattern.endswith("/*")
        else lowered == pattern.lower()
    )


def _role_matches(profile: CredentialProfile, role: CredentialRole) -> bool:
    if role is CredentialRole.AGENT:
        return profile.actor_class is ActorClass.AUTONOMOUS_AGENT
    return profile.actor_class in {ActorClass.HUMAN_OPERATED, ActorClass.DELEGATED_HUMAN}


@dataclass(frozen=True, slots=True)
class RepositoryIdentityObservation:
    provider: RepositoryProvider
    provider_host: str
    repository_id: str
    canonical_name: str
    exists: bool
    observed_at: str
    config_revision: str

    def __post_init__(self) -> None:
        if not isinstance(self.provider, RepositoryProvider):
            raise ValueError("provider must be a RepositoryProvider")
        _host(self.provider_host)
        _safe_id(self.repository_id, "repository_id")
        _canonical_parts(self.canonical_name, expected_host=self.provider_host)
        if not isinstance(self.exists, bool):
            raise ValueError("exists must be boolean")
        _timestamp(self.observed_at, "observed_at")
        _sha256(self.config_revision, "config_revision")


@dataclass(frozen=True, slots=True)
class CredentialProfileEligibility:
    profile: CredentialProfile
    enabled: bool
    repository_patterns: tuple[str, ...]
    boundary_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.profile, CredentialProfile):
            raise ValueError("profile must be a CredentialProfile")
        if not isinstance(self.enabled, bool):
            raise ValueError("enabled must be boolean")
        if (
            not isinstance(self.repository_patterns, tuple)
            or not self.repository_patterns
            or len(self.repository_patterns) > 64
        ):
            raise ValueError("repository_patterns must be a bounded non-empty tuple")
        _safe_id(self.boundary_id, "boundary_id")
        boundaries = tuple(_pattern_boundary(pattern) for pattern in self.repository_patterns)
        if len(set(boundaries)) != 1:
            raise ValueError("repository patterns must remain inside one owner boundary")
        if len(set(self.repository_patterns)) != len(self.repository_patterns):
            raise ValueError("repository_patterns must be unique")

    def matches(self, observation: RepositoryIdentityObservation) -> bool:
        return self.profile.provider is observation.provider and any(
            _pattern_matches(pattern, observation.canonical_name)
            for pattern in self.repository_patterns
        )


@dataclass(frozen=True, slots=True)
class RepositoryBindingSnapshot:
    binding: RepositoryIdentityBinding
    revision: Revision

    def __post_init__(self) -> None:
        if not isinstance(self.binding, RepositoryIdentityBinding):
            raise ValueError("binding must be a RepositoryIdentityBinding")
        if not isinstance(self.revision, Revision):
            raise ValueError("revision must be a Revision")


@dataclass(frozen=True, slots=True)
class RepositoryReconciliationEvent:
    kind: RepositoryReconciliationKind
    previous_canonical_name: str
    current_canonical_name: str

    def __post_init__(self) -> None:
        if not isinstance(self.kind, RepositoryReconciliationKind):
            raise ValueError("kind must be a RepositoryReconciliationKind")
        _canonical_parts(self.previous_canonical_name)
        _canonical_parts(self.current_canonical_name)

    def payload(self) -> dict[str, str]:
        return {
            "kind": self.kind.value,
            "previous_canonical_name": self.previous_canonical_name,
            "current_canonical_name": self.current_canonical_name,
        }


@dataclass(frozen=True, slots=True)
class RepositoryBindingProposal:
    profile_id: str
    binding: RepositoryIdentityBinding

    def __post_init__(self) -> None:
        _safe_id(self.profile_id, "profile_id")
        if not isinstance(self.binding, RepositoryIdentityBinding):
            raise ValueError("binding must be a RepositoryIdentityBinding")

    def payload(self) -> dict[str, object]:
        return {"profile_id": self.profile_id, "binding": self.binding.payload()}


@dataclass(frozen=True, slots=True)
class RepositoryResolutionEvidence:
    provider_host: str
    repository_id: str
    canonical_name: str
    profile_id: str | None
    binding_revision: Revision | None
    outcome: RepositoryResolutionOutcome

    def __post_init__(self) -> None:
        _host(self.provider_host)
        _safe_id(self.repository_id, "repository_id")
        _canonical_parts(self.canonical_name, expected_host=self.provider_host)
        if self.profile_id is not None:
            _safe_id(self.profile_id, "profile_id")
        if self.binding_revision is not None and not isinstance(self.binding_revision, Revision):
            raise ValueError("binding_revision must be a Revision or None")
        if not isinstance(self.outcome, RepositoryResolutionOutcome):
            raise ValueError("outcome must be a RepositoryResolutionOutcome")

    def payload(self) -> dict[str, object]:
        return {
            "provider_host": self.provider_host,
            "repository_id": self.repository_id,
            "canonical_name": self.canonical_name,
            "profile_id": self.profile_id,
            "binding_revision": self.binding_revision.value if self.binding_revision else None,
            "outcome": self.outcome.value,
        }


@dataclass(frozen=True, slots=True)
class RepositoryIdentityResolution:
    outcome: RepositoryResolutionOutcome
    profile: CredentialProfile | None
    binding: RepositoryIdentityBinding | None
    binding_revision: Revision | None
    proposal: RepositoryBindingProposal | None
    reconciliation: RepositoryReconciliationEvent | None
    failure: RepositoryAuthFailure | None
    evidence: RepositoryResolutionEvidence

    def __post_init__(self) -> None:
        if self.outcome is RepositoryResolutionOutcome.RESOLVED:
            if self.profile is None or self.binding is None or self.failure is not None:
                raise ValueError("resolved outcome requires profile and binding only")
        elif self.outcome is RepositoryResolutionOutcome.PROPOSAL_REQUIRED:
            if self.proposal is None or self.failure is not None:
                raise ValueError("proposal outcome requires a proposal without failure")
        elif self.failure is None:
            raise ValueError("failed outcome requires a typed failure")

    def payload(self) -> dict[str, object]:
        return {
            "outcome": self.outcome.value,
            "profile_id": self.profile.profile_id if self.profile else None,
            "binding": self.binding.payload() if self.binding else None,
            "binding_revision": self.binding_revision.value if self.binding_revision else None,
            "proposal": self.proposal.payload() if self.proposal else None,
            "reconciliation": self.reconciliation.payload() if self.reconciliation else None,
            "failure": self.failure.payload() if self.failure else None,
            "evidence": self.evidence.payload(),
        }


def _evidence(
    observation: RepositoryIdentityObservation,
    outcome: RepositoryResolutionOutcome,
    *,
    profile_id: str | None = None,
    binding_revision: Revision | None = None,
) -> RepositoryResolutionEvidence:
    return RepositoryResolutionEvidence(
        provider_host=observation.provider_host,
        repository_id=observation.repository_id,
        canonical_name=observation.canonical_name,
        profile_id=profile_id,
        binding_revision=binding_revision,
        outcome=outcome,
    )


def _failed(
    observation: RepositoryIdentityObservation,
    code: RepositoryAuthFailureCode,
    message: str,
    *,
    retryable: bool = False,
    actions: tuple[RecoveryAction, ...] = (),
    binding_revision: Revision | None = None,
) -> RepositoryIdentityResolution:
    outcome = RepositoryResolutionOutcome.FAILED
    return RepositoryIdentityResolution(
        outcome=outcome,
        profile=None,
        binding=None,
        binding_revision=binding_revision,
        proposal=None,
        reconciliation=None,
        failure=RepositoryAuthFailure(code, message, retryable, actions),
        evidence=_evidence(observation, outcome, binding_revision=binding_revision),
    )


def _profile_for_exact_binding(
    observation: RepositoryIdentityObservation,
    role: CredentialRole,
    binding: RepositoryIdentityBinding,
    profiles: tuple[CredentialProfileEligibility, ...],
    revision: Revision,
) -> RepositoryIdentityResolution | CredentialProfile:
    profile_id = (
        binding.agent_profile_id if role is CredentialRole.AGENT else binding.human_profile_id
    )
    if profile_id is None:
        return _failed(
            observation,
            RepositoryAuthFailureCode.PROFILE_NOT_FOUND,
            "The repository binding has no profile for the requested actor role.",
            actions=(RecoveryAction(RecoveryActionKind.RESELECT_PROFILE),),
            binding_revision=revision,
        )
    matches = tuple(item for item in profiles if item.profile.profile_id == profile_id)
    if len(matches) != 1:
        code = (
            RepositoryAuthFailureCode.PROFILE_AMBIGUOUS
            if len(matches) > 1
            else RepositoryAuthFailureCode.PROFILE_NOT_FOUND
        )
        return _failed(
            observation,
            code,
            "The bound credential profile is missing or non-unique.",
            actions=(RecoveryAction(RecoveryActionKind.RESELECT_PROFILE),),
            binding_revision=revision,
        )
    eligibility = matches[0]
    if not eligibility.enabled:
        return _failed(
            observation,
            RepositoryAuthFailureCode.PROFILE_NOT_AUTHORIZED,
            "The bound credential profile is disabled.",
            actions=(RecoveryAction(RecoveryActionKind.REAUTHORIZE),),
            binding_revision=revision,
        )
    if eligibility.profile.provider is not observation.provider or not _role_matches(
        eligibility.profile, role
    ):
        return _failed(
            observation,
            RepositoryAuthFailureCode.PROFILE_NOT_AUTHORIZED,
            "The bound credential profile is incompatible with the requested provider or actor role.",
            actions=(RecoveryAction(RecoveryActionKind.RESELECT_PROFILE),),
            binding_revision=revision,
        )
    return eligibility.profile


def resolve_repository_identity(
    *,
    observation: RepositoryIdentityObservation,
    role: CredentialRole,
    bindings: tuple[RepositoryBindingSnapshot, ...],
    profiles: tuple[CredentialProfileEligibility, ...],
    expected_binding_revision: Revision | None = None,
) -> RepositoryIdentityResolution:
    """Resolve one repository without global account state or implicit profile fallback."""

    if not isinstance(role, CredentialRole):
        raise ValueError("role must be a CredentialRole")
    if not isinstance(bindings, tuple) or any(
        not isinstance(item, RepositoryBindingSnapshot) for item in bindings
    ):
        raise ValueError("bindings must contain RepositoryBindingSnapshot values")
    if not isinstance(profiles, tuple) or any(
        not isinstance(item, CredentialProfileEligibility) for item in profiles
    ):
        raise ValueError("profiles must contain CredentialProfileEligibility values")
    if not observation.exists:
        return _failed(
            observation,
            RepositoryAuthFailureCode.REPOSITORY_BINDING_NOT_FOUND,
            "The provider did not confirm that the target repository still exists.",
            actions=(RecoveryAction(RecoveryActionKind.RECONCILE_BINDING),),
        )

    exact = tuple(
        item
        for item in bindings
        if item.binding.provider is observation.provider
        and item.binding.provider_host == observation.provider_host
        and item.binding.repository_id == observation.repository_id
    )
    if len(exact) > 1:
        return _failed(
            observation,
            RepositoryAuthFailureCode.BINDING_AMBIGUOUS,
            "More than one binding claims the same stable repository identity.",
            actions=(RecoveryAction(RecoveryActionKind.RECONCILE_BINDING),),
        )
    if exact:
        snapshot = exact[0]
        binding = snapshot.binding
        if expected_binding_revision is not None and snapshot.revision != expected_binding_revision:
            return _failed(
                observation,
                RepositoryAuthFailureCode.BINDING_STALE,
                "The repository binding revision changed after it was reviewed.",
                retryable=True,
                actions=(RecoveryAction(RecoveryActionKind.RECONCILE_BINDING),),
                binding_revision=snapshot.revision,
            )
        if binding.config_revision != observation.config_revision:
            return _failed(
                observation,
                RepositoryAuthFailureCode.BINDING_STALE,
                "The repository binding belongs to a different configuration revision.",
                retryable=True,
                actions=(RecoveryAction(RecoveryActionKind.RECONCILE_BINDING),),
                binding_revision=snapshot.revision,
            )
        reconciliation: RepositoryReconciliationEvent | None = None
        if binding.canonical_name != observation.canonical_name:
            if _owner_boundary(binding.canonical_name) != _owner_boundary(
                observation.canonical_name
            ):
                return _failed(
                    observation,
                    RepositoryAuthFailureCode.BINDING_REPOSITORY_MISMATCH,
                    "The stable repository moved across an owner policy boundary.",
                    actions=(RecoveryAction(RecoveryActionKind.RECONCILE_BINDING),),
                    binding_revision=snapshot.revision,
                )
            reconciliation = RepositoryReconciliationEvent(
                RepositoryReconciliationKind.RENAMED,
                binding.canonical_name,
                observation.canonical_name,
            )
        selected = _profile_for_exact_binding(
            observation, role, binding, profiles, snapshot.revision
        )
        if isinstance(selected, RepositoryIdentityResolution):
            return selected
        outcome = RepositoryResolutionOutcome.RESOLVED
        return RepositoryIdentityResolution(
            outcome=outcome,
            profile=selected,
            binding=binding,
            binding_revision=snapshot.revision,
            proposal=None,
            reconciliation=reconciliation,
            failure=None,
            evidence=_evidence(
                observation,
                outcome,
                profile_id=selected.profile_id,
                binding_revision=snapshot.revision,
            ),
        )

    canonical_collision = tuple(
        item
        for item in bindings
        if item.binding.provider is observation.provider
        and item.binding.provider_host == observation.provider_host
        and item.binding.canonical_name.lower() == observation.canonical_name.lower()
        and item.binding.repository_id != observation.repository_id
    )
    if canonical_collision:
        return _failed(
            observation,
            RepositoryAuthFailureCode.BINDING_REPOSITORY_MISMATCH,
            "The repository name now resolves to a different stable repository ID.",
            actions=(RecoveryAction(RecoveryActionKind.RECONCILE_BINDING),),
        )

    matching = tuple(
        item for item in profiles if item.matches(observation) and _role_matches(item.profile, role)
    )
    eligible = tuple(item for item in matching if item.enabled)
    if len(eligible) > 1:
        return _failed(
            observation,
            RepositoryAuthFailureCode.PROFILE_AMBIGUOUS,
            "More than one credential profile is eligible for the unbound repository.",
            actions=(RecoveryAction(RecoveryActionKind.RESELECT_PROFILE),),
        )
    if not eligible:
        code = (
            RepositoryAuthFailureCode.PROFILE_NOT_AUTHORIZED
            if matching
            else RepositoryAuthFailureCode.PROFILE_NOT_FOUND
        )
        action = RecoveryActionKind.REAUTHORIZE if matching else RecoveryActionKind.RESELECT_PROFILE
        return _failed(
            observation,
            code,
            "No enabled credential profile is eligible for the unbound repository.",
            actions=(RecoveryAction(action),),
        )

    profile = eligible[0].profile
    binding = RepositoryIdentityBinding(
        provider=observation.provider,
        repository_id=observation.repository_id,
        canonical_name=observation.canonical_name,
        human_profile_id=profile.profile_id if role is CredentialRole.HUMAN else None,
        agent_profile_id=profile.profile_id if role is CredentialRole.AGENT else None,
        config_revision=observation.config_revision,
        provider_host=observation.provider_host,
    )
    outcome = RepositoryResolutionOutcome.PROPOSAL_REQUIRED
    proposal = RepositoryBindingProposal(profile.profile_id, binding)
    return RepositoryIdentityResolution(
        outcome=outcome,
        profile=None,
        binding=None,
        binding_revision=None,
        proposal=proposal,
        reconciliation=None,
        failure=None,
        evidence=_evidence(observation, outcome, profile_id=profile.profile_id),
    )
