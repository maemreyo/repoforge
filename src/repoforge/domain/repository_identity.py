"""Provider-neutral repository authentication and publication identity contracts.

The domain stores only safe identity metadata and opaque secret references. Provider
adapters own credential bodies, transport configuration, and live proof collection.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SAFE_SCHEME = re.compile(r"^[a-z][a-z0-9+.-]{0,31}$")
_SHA40 = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REPOSITORY_NAME_PART = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
_PROVIDER_HOST = re.compile(r"^[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?$")
_SECRET_REFERENCE = re.compile(
    r"(?:^gh[pousr]_|^github_pat_|^bearer[- _]|private[-_ ]?key|authorization|"
    r"begin [^-\n]{0,32}private key)",
    re.IGNORECASE,
)
_MAX_TEXT = 512


def _safe_id(value: str, field: str) -> str:
    if not isinstance(value, str) or _SAFE_ID.fullmatch(value) is None:
        raise ValueError(f"{field} must be a bounded safe identifier")
    return value


def _sha256(value: str, field: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{field} must be a full lowercase SHA-256")
    return value


def _sha40(value: str, field: str) -> str:
    if not isinstance(value, str) or _SHA40.fullmatch(value) is None:
        raise ValueError(f"{field} must be a full lowercase Git object ID")
    return value


def _bounded_text(value: str, field: str, *, maximum: int = _MAX_TEXT) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or any(ord(character) < 32 for character in value)
    ):
        raise ValueError(f"{field} must be bounded non-empty text without control characters")
    return value


def _optional_safe_id(value: str | None, field: str) -> str | None:
    return None if value is None else _safe_id(value, field)


def _timestamp(value: str, field: str) -> datetime:
    if not isinstance(value, str) or len(value) > 80:
        raise ValueError(f"{field} must be a bounded timezone-aware timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must include a timezone offset")
    return parsed


def _unique_safe_ids(values: tuple[str, ...], field: str, *, maximum: int = 64) -> tuple[str, ...]:
    if not isinstance(values, tuple) or len(values) > maximum:
        raise ValueError(f"{field} must be a tuple with at most {maximum} entries")
    normalized = tuple(_safe_id(item, field) for item in values)
    if len(set(normalized)) != len(normalized):
        raise ValueError(f"{field} entries must be unique")
    return normalized


def _git_ref(value: str | None, field: str) -> str | None:
    if value is None:
        return None
    _bounded_text(value, field, maximum=1_024)
    if (
        value.startswith(("/", "."))
        or value.endswith(("/", "."))
        or ".." in value
        or "@{" in value
        or "\\" in value
        or any(character.isspace() for character in value)
    ):
        raise ValueError(f"{field} is not a safe exact Git ref")
    return value


class RepositoryProvider(str, Enum):
    GITHUB = "github"


class ActorClass(str, Enum):
    HUMAN_OPERATED = "human_operated"
    DELEGATED_HUMAN = "delegated_human"
    AUTONOMOUS_AGENT = "autonomous_agent"


class CredentialKind(str, Enum):
    STORED_ACCOUNT = "stored_account"
    GITHUB_APP = "github_app"
    WORKLOAD_IDENTITY = "workload_identity"


class AuthLeaseState(str, Enum):
    ACTIVE = "active"
    EXPIRED = "expired"
    REVOKED = "revoked"
    RELEASED = "released"


class AuthTargetKind(str, Enum):
    REPOSITORY = "repository"
    SUBMODULE = "submodule"
    LFS = "lfs"
    PACKAGE = "package"
    RELEASE = "release"


class PublicationKind(str, Enum):
    GIT_PUSH = "git_push"
    PULL_REQUEST = "pull_request"
    RELEASE = "release"


class IdentitySurface(str, Enum):
    REPOSITORY_BINDING = "repository_binding"
    GITHUB_API = "github_api"
    GIT_FETCH = "git_fetch"
    GIT_PUSH = "git_push"
    COMMIT_AUTHOR = "commit_author"
    COMMIT_COMMITTER = "commit_committer"
    COMMIT_SIGNER = "commit_signer"
    PULL_REQUEST = "pull_request"
    RELEASE = "release"
    SUBMODULE = "submodule"
    LFS = "lfs"
    PACKAGE = "package"


class IdentityEvidenceKind(str, Enum):
    VERIFIED_ACTOR = "verified_actor"
    VERIFIED_REPOSITORY = "verified_repository"
    TRANSPORT_ACCESS_PROOF = "transport_access_proof"
    CONFIGURED_METADATA = "configured_metadata"
    UNOBSERVABLE = "unobservable"


class RepositoryAuthFailureCode(str, Enum):
    PROFILE_NOT_FOUND = "PROFILE_NOT_FOUND"
    PROFILE_AMBIGUOUS = "PROFILE_AMBIGUOUS"
    PROFILE_NOT_AUTHORIZED = "PROFILE_NOT_AUTHORIZED"
    REPOSITORY_BINDING_NOT_FOUND = "REPOSITORY_BINDING_NOT_FOUND"
    BINDING_AMBIGUOUS = "BINDING_AMBIGUOUS"
    BINDING_STALE = "BINDING_STALE"
    BINDING_REPOSITORY_MISMATCH = "BINDING_REPOSITORY_MISMATCH"
    ACTOR_MISMATCH = "ACTOR_MISMATCH"
    TRANSPORT_PROOF_UNAVAILABLE = "TRANSPORT_PROOF_UNAVAILABLE"
    TRANSPORT_IDENTITY_MISMATCH = "TRANSPORT_IDENTITY_MISMATCH"
    LEASE_EXPIRED = "LEASE_EXPIRED"
    LEASE_REVOKED = "LEASE_REVOKED"
    LEASE_REFRESH_FAILED = "LEASE_REFRESH_FAILED"
    REMOTE_REWRITE_DETECTED = "REMOTE_REWRITE_DETECTED"
    PUBLICATION_TARGET_MISMATCH = "PUBLICATION_TARGET_MISMATCH"
    CROSS_BOUNDARY_PUBLICATION_DENIED = "CROSS_BOUNDARY_PUBLICATION_DENIED"
    NESTED_RESOURCE_BINDING_REQUIRED = "NESTED_RESOURCE_BINDING_REQUIRED"
    NESTED_RESOURCE_DENIED = "NESTED_RESOURCE_DENIED"
    AUTHOR_MISMATCH = "AUTHOR_MISMATCH"
    COMMITTER_MISMATCH = "COMMITTER_MISMATCH"
    SIGNER_MISMATCH = "SIGNER_MISMATCH"
    SSO_AUTHORIZATION_REQUIRED = "SSO_AUTHORIZATION_REQUIRED"
    NETWORK_POLICY_DENIED = "NETWORK_POLICY_DENIED"


class RecoveryActionKind(str, Enum):
    RESELECT_PROFILE = "reselect_profile"
    REAUTHORIZE = "reauthorize"
    REFRESH_LEASE = "refresh_lease"
    RECONCILE_BINDING = "reconcile_binding"
    REVIEW_REMOTE = "review_remote"
    REVERIFY_ACTOR = "reverify_actor"
    RETRY_AFTER_NETWORK = "retry_after_network"
    REQUEST_CAPABILITY = "request_capability"
    ABORT = "abort"


@dataclass(frozen=True, slots=True)
class OpaqueCredentialReference:
    scheme: str
    reference_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.scheme, str) or _SAFE_SCHEME.fullmatch(self.scheme) is None:
            raise ValueError("opaque credential reference scheme is invalid")
        if (
            not isinstance(self.reference_id, str)
            or _SAFE_ID.fullmatch(self.reference_id) is None
            or _SECRET_REFERENCE.search(self.reference_id) is not None
        ):
            raise ValueError("opaque credential reference must be a safe non-secret identifier")

    def payload(self) -> dict[str, str]:
        return {"scheme": self.scheme, "reference_id": self.reference_id}


@dataclass(frozen=True, slots=True)
class CredentialProfile:
    profile_id: str
    provider: RepositoryProvider
    credential_kind: CredentialKind
    credential_ref: OpaqueCredentialReference
    actor_class: ActorClass
    expected_actor_id: str | None
    capability_ids: tuple[str, ...]
    revision: str

    def __post_init__(self) -> None:
        _safe_id(self.profile_id, "profile_id")
        if not isinstance(self.provider, RepositoryProvider):
            raise ValueError("provider must be a RepositoryProvider")
        if not isinstance(self.credential_kind, CredentialKind):
            raise ValueError("credential_kind must be a CredentialKind")
        if not isinstance(self.credential_ref, OpaqueCredentialReference):
            raise ValueError("credential_ref must be an OpaqueCredentialReference")
        if not isinstance(self.actor_class, ActorClass):
            raise ValueError("actor_class must be an ActorClass")
        _optional_safe_id(self.expected_actor_id, "expected_actor_id")
        _unique_safe_ids(self.capability_ids, "capability_ids")
        _sha256(self.revision, "revision")

    def payload(self) -> dict[str, object]:
        return {
            "profile_id": self.profile_id,
            "provider": self.provider.value,
            "credential_kind": self.credential_kind.value,
            "credential_ref": self.credential_ref.payload(),
            "actor_class": self.actor_class.value,
            "expected_actor_id": self.expected_actor_id,
            "capability_ids": list(self.capability_ids),
            "revision": self.revision,
        }


@dataclass(frozen=True, slots=True)
class RepositoryIdentityBinding:
    provider: RepositoryProvider
    repository_id: str
    canonical_name: str
    human_profile_id: str | None
    agent_profile_id: str | None
    config_revision: str
    provider_host: str = "github.com"

    def __post_init__(self) -> None:
        if not isinstance(self.provider, RepositoryProvider):
            raise ValueError("provider must be a RepositoryProvider")
        if (
            not isinstance(self.provider_host, str)
            or _PROVIDER_HOST.fullmatch(self.provider_host) is None
        ):
            raise ValueError("provider_host must be a bounded lowercase host")
        _safe_id(self.repository_id, "repository_id")
        canonical_parts = self.canonical_name.split("/")
        if (
            len(canonical_parts) != 3
            or canonical_parts[0] != self.provider_host
            or _REPOSITORY_NAME_PART.fullmatch(canonical_parts[1]) is None
            or _REPOSITORY_NAME_PART.fullmatch(canonical_parts[2]) is None
        ):
            raise ValueError("canonical_name must be provider_host/<owner>/<repository>")
        _optional_safe_id(self.human_profile_id, "human_profile_id")
        _optional_safe_id(self.agent_profile_id, "agent_profile_id")
        if self.human_profile_id is None and self.agent_profile_id is None:
            raise ValueError("repository binding requires a human or agent profile")
        _sha256(self.config_revision, "config_revision")

    def payload(self) -> dict[str, object]:
        return {
            "provider": self.provider.value,
            "repository_id": self.repository_id,
            "canonical_name": self.canonical_name,
            "human_profile_id": self.human_profile_id,
            "agent_profile_id": self.agent_profile_id,
            "config_revision": self.config_revision,
            "provider_host": self.provider_host,
        }


@dataclass(frozen=True, slots=True)
class AuthLease:
    lease_id: str
    profile_id: str
    provider: RepositoryProvider
    repository_id: str
    target_kind: AuthTargetKind
    target_id: str
    actor_id: str | None
    credential_ref: OpaqueCredentialReference
    issued_at: str
    expires_at: str
    state: AuthLeaseState
    config_revision: str
    policy_revision: str
    material_digest: str | None = None
    provider_metadata: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        _safe_id(self.lease_id, "lease_id")
        _safe_id(self.profile_id, "profile_id")
        if not isinstance(self.provider, RepositoryProvider):
            raise ValueError("provider must be a RepositoryProvider")
        _safe_id(self.repository_id, "repository_id")
        if not isinstance(self.target_kind, AuthTargetKind):
            raise ValueError("target_kind must be an AuthTargetKind")
        _bounded_text(self.target_id, "target_id")
        _optional_safe_id(self.actor_id, "actor_id")
        if not isinstance(self.credential_ref, OpaqueCredentialReference):
            raise ValueError("credential_ref must be an OpaqueCredentialReference")
        issued = _timestamp(self.issued_at, "issued_at")
        expires = _timestamp(self.expires_at, "expires_at")
        if expires <= issued:
            raise ValueError("expires_at must be later than issued_at")
        if not isinstance(self.state, AuthLeaseState):
            raise ValueError("state must be an AuthLeaseState")
        _sha256(self.config_revision, "config_revision")
        _sha256(self.policy_revision, "policy_revision")
        if self.material_digest is not None:
            _sha256(self.material_digest, "material_digest")
        if not isinstance(self.provider_metadata, tuple) or len(self.provider_metadata) > 16:
            raise ValueError("provider_metadata must be a bounded tuple")
        metadata_names: list[str] = []
        for name, value in self.provider_metadata:
            metadata_names.append(_safe_id(name, "provider metadata name"))
            _bounded_text(value, "provider metadata value")
        if len(set(metadata_names)) != len(metadata_names):
            raise ValueError("provider metadata names must be unique")
        # provider_metadata is read everywhere as dict(lease.provider_metadata) --
        # name order carries no meaning. But it's still a tuple, so `==` (used to
        # detect a lease changing between authorization and effect) IS
        # order-sensitive, and the durable store round-trips it sorted
        # (json_operation_identity_store.py) while a freshly issued lease keeps
        # construction order. Canonicalize here so every AuthLease, regardless of
        # where it was built, compares equal when its *contents* match.
        object.__setattr__(self, "provider_metadata", tuple(sorted(self.provider_metadata)))

    def payload(self) -> dict[str, object]:
        return {
            "lease_id": self.lease_id,
            "profile_id": self.profile_id,
            "provider": self.provider.value,
            "repository_id": self.repository_id,
            "target_kind": self.target_kind.value,
            "target_id": self.target_id,
            "actor_id": self.actor_id,
            "credential_ref": self.credential_ref.payload(),
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "state": self.state.value,
            "config_revision": self.config_revision,
            "policy_revision": self.policy_revision,
            "material_digest": self.material_digest,
            "provider_metadata": dict(self.provider_metadata),
        }


@dataclass(frozen=True, slots=True)
class OperationIdentityContext:
    operation_id: str
    primary_repository_id: str
    actor_class: ActorClass
    auth_leases: tuple[AuthLease, ...]
    selected_at: str
    config_revision: str
    policy_revision: str

    def __post_init__(self) -> None:
        _safe_id(self.operation_id, "operation_id")
        _safe_id(self.primary_repository_id, "primary_repository_id")
        if not isinstance(self.actor_class, ActorClass):
            raise ValueError("actor_class must be an ActorClass")
        if not isinstance(self.auth_leases, tuple) or not self.auth_leases:
            raise ValueError("auth_leases must contain at least one target-bound lease")
        if any(not isinstance(item, AuthLease) for item in self.auth_leases):
            raise ValueError("auth_leases must contain AuthLease values")
        lease_ids = tuple(item.lease_id for item in self.auth_leases)
        target_keys = tuple((item.target_kind, item.target_id) for item in self.auth_leases)
        if len(set(lease_ids)) != len(lease_ids):
            raise ValueError("auth lease IDs must be unique")
        if len(set(target_keys)) != len(target_keys):
            raise ValueError("each operation target must have one pinned auth lease")
        if not any(item.repository_id == self.primary_repository_id for item in self.auth_leases):
            raise ValueError("primary repository must have a pinned auth lease")
        _timestamp(self.selected_at, "selected_at")
        _sha256(self.config_revision, "config_revision")
        _sha256(self.policy_revision, "policy_revision")

    def payload(self) -> dict[str, object]:
        return {
            "operation_id": self.operation_id,
            "primary_repository_id": self.primary_repository_id,
            "actor_class": self.actor_class.value,
            "auth_leases": [item.payload() for item in self.auth_leases],
            "selected_at": self.selected_at,
            "config_revision": self.config_revision,
            "policy_revision": self.policy_revision,
        }


@dataclass(frozen=True, slots=True)
class PublicationIntent:
    publication_id: str
    operation_id: str
    kind: PublicationKind
    source_repository_id: str
    destination_repository_id: str
    remote_name: str
    source_ref: str
    destination_ref: str
    expected_commit_sha: str
    expected_tree_sha: str | None = None
    base_ref: str | None = None
    head_ref: str | None = None
    cross_boundary_approval_id: str | None = None

    def __post_init__(self) -> None:
        _safe_id(self.publication_id, "publication_id")
        _safe_id(self.operation_id, "operation_id")
        if not isinstance(self.kind, PublicationKind):
            raise ValueError("kind must be a PublicationKind")
        _safe_id(self.source_repository_id, "source_repository_id")
        _safe_id(self.destination_repository_id, "destination_repository_id")
        _safe_id(self.remote_name, "remote_name")
        _git_ref(self.source_ref, "source_ref")
        _git_ref(self.destination_ref, "destination_ref")
        _git_ref(self.base_ref, "base_ref")
        _git_ref(self.head_ref, "head_ref")
        _sha40(self.expected_commit_sha, "expected_commit_sha")
        if self.expected_tree_sha is not None:
            _sha40(self.expected_tree_sha, "expected_tree_sha")
        _optional_safe_id(self.cross_boundary_approval_id, "cross_boundary_approval_id")
        if (
            self.source_repository_id != self.destination_repository_id
            and self.cross_boundary_approval_id is None
        ):
            raise ValueError("cross-boundary publication requires an explicit approval ID")
        if self.kind is PublicationKind.PULL_REQUEST and (
            self.base_ref is None or self.head_ref is None
        ):
            raise ValueError("pull request publication requires exact base_ref and head_ref")

    @property
    def exact_refspec(self) -> str:
        return f"{self.source_ref}:{self.destination_ref}"

    def payload(self) -> dict[str, object]:
        return {
            "publication_id": self.publication_id,
            "operation_id": self.operation_id,
            "kind": self.kind.value,
            "source_repository_id": self.source_repository_id,
            "destination_repository_id": self.destination_repository_id,
            "remote_name": self.remote_name,
            "source_ref": self.source_ref,
            "destination_ref": self.destination_ref,
            "exact_refspec": self.exact_refspec,
            "base_ref": self.base_ref,
            "head_ref": self.head_ref,
            "expected_commit_sha": self.expected_commit_sha,
            "expected_tree_sha": self.expected_tree_sha,
            "cross_boundary_approval_id": self.cross_boundary_approval_id,
        }


@dataclass(frozen=True, slots=True)
class IdentitySurfaceEvidence:
    surface: IdentitySurface
    evidence_kind: IdentityEvidenceKind
    repository_id: str
    profile_id: str
    actor_id: str | None
    target: str
    observed_at: str
    evidence_digest: str

    def __post_init__(self) -> None:
        if not isinstance(self.surface, IdentitySurface):
            raise ValueError("surface must be an IdentitySurface")
        if not isinstance(self.evidence_kind, IdentityEvidenceKind):
            raise ValueError("evidence_kind must be an IdentityEvidenceKind")
        _safe_id(self.repository_id, "repository_id")
        _safe_id(self.profile_id, "profile_id")
        _optional_safe_id(self.actor_id, "actor_id")
        _bounded_text(self.target, "target", maximum=1_024)
        _timestamp(self.observed_at, "observed_at")
        _sha256(self.evidence_digest, "evidence_digest")
        if self.evidence_kind is IdentityEvidenceKind.VERIFIED_ACTOR and self.actor_id is None:
            raise ValueError("verified actor evidence requires actor_id")

    @property
    def proves_actor(self) -> bool:
        return (
            self.evidence_kind is IdentityEvidenceKind.VERIFIED_ACTOR and self.actor_id is not None
        )

    def payload(self) -> dict[str, object]:
        return {
            "surface": self.surface.value,
            "evidence_kind": self.evidence_kind.value,
            "repository_id": self.repository_id,
            "profile_id": self.profile_id,
            "actor_id": self.actor_id,
            "target": self.target,
            "observed_at": self.observed_at,
            "evidence_digest": self.evidence_digest,
            "proves_actor": self.proves_actor,
        }


@dataclass(frozen=True, slots=True)
class IdentityReceipt:
    receipt_id: str
    operation_id: str
    repository_id: str
    actor_class: ActorClass
    profile_ids: tuple[str, ...]
    lease_ids: tuple[str, ...]
    evidence: tuple[IdentitySurfaceEvidence, ...]
    publication_intent_id: str | None
    commit_sha: str | None
    tree_sha: str | None
    exact_ref: str | None
    config_revision: str
    policy_revision: str
    created_at: str
    outcome: str

    def __post_init__(self) -> None:
        _safe_id(self.receipt_id, "receipt_id")
        _safe_id(self.operation_id, "operation_id")
        _safe_id(self.repository_id, "repository_id")
        if not isinstance(self.actor_class, ActorClass):
            raise ValueError("actor_class must be an ActorClass")
        _unique_safe_ids(self.profile_ids, "profile_ids")
        _unique_safe_ids(self.lease_ids, "lease_ids")
        if not isinstance(self.evidence, tuple) or not self.evidence:
            raise ValueError("identity receipt requires surface evidence")
        if any(not isinstance(item, IdentitySurfaceEvidence) for item in self.evidence):
            raise ValueError("evidence must contain IdentitySurfaceEvidence values")
        if any(item.repository_id != self.repository_id for item in self.evidence):
            raise ValueError("receipt evidence must bind the receipt repository")
        _optional_safe_id(self.publication_intent_id, "publication_intent_id")
        if self.commit_sha is not None:
            _sha40(self.commit_sha, "commit_sha")
        if self.tree_sha is not None:
            _sha40(self.tree_sha, "tree_sha")
        _git_ref(self.exact_ref, "exact_ref")
        _sha256(self.config_revision, "config_revision")
        _sha256(self.policy_revision, "policy_revision")
        _timestamp(self.created_at, "created_at")
        _safe_id(self.outcome, "outcome")

    def payload(self) -> dict[str, object]:
        return {
            "receipt_id": self.receipt_id,
            "operation_id": self.operation_id,
            "repository_id": self.repository_id,
            "actor_class": self.actor_class.value,
            "profile_ids": list(self.profile_ids),
            "lease_ids": list(self.lease_ids),
            "evidence": [item.payload() for item in self.evidence],
            "publication_intent_id": self.publication_intent_id,
            "commit_sha": self.commit_sha,
            "tree_sha": self.tree_sha,
            "exact_ref": self.exact_ref,
            "config_revision": self.config_revision,
            "policy_revision": self.policy_revision,
            "created_at": self.created_at,
            "outcome": self.outcome,
        }


@dataclass(frozen=True, slots=True)
class RecoveryAction:
    kind: RecoveryActionKind
    parameters: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.kind, RecoveryActionKind):
            raise ValueError("kind must be a RecoveryActionKind")
        if not isinstance(self.parameters, tuple) or len(self.parameters) > 16:
            raise ValueError("recovery action parameters must be a bounded tuple")
        names: list[str] = []
        for name, value in self.parameters:
            names.append(_safe_id(name, "recovery parameter name"))
            _bounded_text(value, "recovery parameter value")
        if len(set(names)) != len(names):
            raise ValueError("recovery action parameter names must be unique")

    def payload(self) -> dict[str, object]:
        return {"kind": self.kind.value, "parameters": dict(self.parameters)}


@dataclass(frozen=True, slots=True)
class RepositoryAuthFailure:
    code: RepositoryAuthFailureCode
    message: str
    retryable: bool
    recovery_actions: tuple[RecoveryAction, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.code, RepositoryAuthFailureCode):
            raise ValueError("code must be a RepositoryAuthFailureCode")
        _bounded_text(self.message, "message", maximum=2_000)
        if not isinstance(self.retryable, bool):
            raise ValueError("retryable must be boolean")
        if not isinstance(self.recovery_actions, tuple) or len(self.recovery_actions) > 8:
            raise ValueError("recovery_actions must be a bounded tuple")
        if any(not isinstance(item, RecoveryAction) for item in self.recovery_actions):
            raise ValueError("recovery_actions must contain RecoveryAction values")

    def payload(self) -> dict[str, object]:
        return {
            "code": self.code.value,
            "message": self.message,
            "retryable": self.retryable,
            "recovery_actions": [item.payload() for item in self.recovery_actions],
        }


def identity_receipt_payload(receipt: IdentityReceipt) -> dict[str, object]:
    """Return deterministic safe metadata for audit, state, and model boundaries."""

    if not isinstance(receipt, IdentityReceipt):
        raise ValueError("receipt must be an IdentityReceipt")
    return receipt.payload()
