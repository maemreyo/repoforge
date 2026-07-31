"""Pure review contracts for exact repository publication effects."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime

from .errors import ErrorCode, RepoForgeError
from .repository_identity import (
    ActorClass,
    AuthLease,
    AuthLeaseState,
    AuthTargetKind,
    IdentityEvidenceKind,
    IdentitySurface,
    IdentitySurfaceEvidence,
    PublicationIntent,
    PublicationKind,
)

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SHA40 = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MAX_URLS = 16


def _safe_id(value: str, field: str) -> str:
    if not isinstance(value, str) or _SAFE_ID.fullmatch(value) is None:
        raise ValueError(f"{field} must be a bounded safe identifier")
    return value


def _bounded_text(value: str, field: str, *, maximum: int = 1_024) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or any(ord(character) < 32 for character in value)
    ):
        raise ValueError(f"{field} must be bounded non-empty text without control characters")
    return value


def _sha40(value: str, field: str) -> str:
    if not isinstance(value, str) or _SHA40.fullmatch(value) is None:
        raise ValueError(f"{field} must be a full lowercase Git object ID")
    return value


def _sha256(value: str, field: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{field} must be a full lowercase SHA-256")
    return value


def _timestamp(value: str, field: str) -> datetime:
    if not isinstance(value, str) or not value or len(value) > 80:
        raise ValueError(f"{field} must be a bounded timezone-aware timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must include a timezone offset")
    return parsed


def _git_ref(value: str | None, field: str) -> str | None:
    if value is None:
        return None
    _bounded_text(value, field)
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


def _publication_error(code: ErrorCode, message: str) -> RepoForgeError:
    return RepoForgeError(
        message,
        code=code,
        retryable=False,
        unchanged_state=("No external publication effect was started.",),
        safe_next_action="Re-inspect the exact repository topology and identity before retrying.",
    )


def _require_exact_managed_ref(value: str, field: str) -> None:
    """Reject revision expressions and broad or deleting refspec components."""

    if (
        value in {"HEAD", "--all", "--mirror"}
        or value.startswith("-")
        or not value.startswith(("refs/heads/", "refs/tags/"))
        or any(marker in value for marker in ("*", "?", "[", ":", "^", "~"))
    ):
        raise _publication_error(
            ErrorCode.PUBLICATION_TARGET_MISMATCH,
            f"{field} must be one exact managed branch or tag ref.",
        )


def _digest(payload: object) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class RepositoryEndpoint:
    repository_id: str
    canonical_name: str
    boundary_id: str
    exact_ref: str | None
    url_digests: tuple[str, ...]

    def __post_init__(self) -> None:
        _safe_id(self.repository_id, "repository_id")
        _bounded_text(self.canonical_name, "canonical_name")
        _safe_id(self.boundary_id, "boundary_id")
        _git_ref(self.exact_ref, "exact_ref")
        if (
            not isinstance(self.url_digests, tuple)
            or not self.url_digests
            or len(self.url_digests) > _MAX_URLS
        ):
            raise ValueError("url_digests must be a non-empty bounded tuple")
        normalized = tuple(_sha256(value, "url_digest") for value in self.url_digests)
        if len(set(normalized)) != len(normalized):
            raise ValueError("url_digests must be unique")

    def payload(self) -> dict[str, object]:
        return {
            "repository_id": self.repository_id,
            "canonical_name": self.canonical_name,
            "boundary_id": self.boundary_id,
            "exact_ref": self.exact_ref,
            "url_digests": list(self.url_digests),
        }


@dataclass(frozen=True, slots=True)
class RemoteTopology:
    remote_name: str
    fetch: RepositoryEndpoint
    push: RepositoryEndpoint
    base: RepositoryEndpoint
    head: RepositoryEndpoint
    source_ref: str
    destination_ref: str
    rewrite_digest: str
    observed_at: str

    def __post_init__(self) -> None:
        _safe_id(self.remote_name, "remote_name")
        for name, endpoint in (
            ("fetch", self.fetch),
            ("push", self.push),
            ("base", self.base),
            ("head", self.head),
        ):
            if not isinstance(endpoint, RepositoryEndpoint):
                raise ValueError(f"{name} must be a RepositoryEndpoint")
        _git_ref(self.source_ref, "source_ref")
        _git_ref(self.destination_ref, "destination_ref")
        _sha256(self.rewrite_digest, "rewrite_digest")
        _timestamp(self.observed_at, "observed_at")

    def review_payload(self) -> dict[str, object]:
        return {
            "remote_name": self.remote_name,
            "fetch": self.fetch.payload(),
            "push": self.push.payload(),
            "base": self.base.payload(),
            "head": self.head.payload(),
            "source_ref": self.source_ref,
            "destination_ref": self.destination_ref,
        }

    def payload(self) -> dict[str, object]:
        return {
            **self.review_payload(),
            "rewrite_digest": self.rewrite_digest,
            "observed_at": self.observed_at,
        }

    @property
    def topology_digest(self) -> str:
        return _digest({**self.review_payload(), "rewrite_digest": self.rewrite_digest})


@dataclass(frozen=True, slots=True)
class PublicationEvidence:
    operation_id: str
    profile_id: str
    actor_class: ActorClass
    actor_id: str
    installation_id: str | None
    lease: AuthLease
    identity_surfaces: tuple[IdentitySurfaceEvidence, ...]
    preflight_topology: RemoteTopology
    observed_topology: RemoteTopology
    observed_commit_sha: str
    observed_tree_sha: str
    expected_capability_digest: str
    observed_capability_digest: str
    expected_permission_digest: str
    observed_permission_digest: str
    expected_remote_version: str
    observed_remote_version: str
    preflight_evidence_digest: str
    approved_cross_boundary_id: str | None
    observed_at: str

    def __post_init__(self) -> None:
        _safe_id(self.operation_id, "operation_id")
        _safe_id(self.profile_id, "profile_id")
        if not isinstance(self.actor_class, ActorClass):
            raise ValueError("actor_class must be an ActorClass")
        _safe_id(self.actor_id, "actor_id")
        if self.installation_id is not None:
            _safe_id(self.installation_id, "installation_id")
        if not isinstance(self.lease, AuthLease):
            raise ValueError("lease must be an AuthLease")
        if not isinstance(self.identity_surfaces, tuple) or not self.identity_surfaces:
            raise ValueError("identity_surfaces must be a non-empty tuple")
        if any(not isinstance(item, IdentitySurfaceEvidence) for item in self.identity_surfaces):
            raise ValueError("identity_surfaces must contain IdentitySurfaceEvidence values")
        if not isinstance(self.preflight_topology, RemoteTopology) or not isinstance(
            self.observed_topology, RemoteTopology
        ):
            raise ValueError("publication evidence requires reviewed remote topology values")
        _sha40(self.observed_commit_sha, "observed_commit_sha")
        _sha40(self.observed_tree_sha, "observed_tree_sha")
        for name, value in (
            ("expected_capability_digest", self.expected_capability_digest),
            ("observed_capability_digest", self.observed_capability_digest),
            ("expected_permission_digest", self.expected_permission_digest),
            ("observed_permission_digest", self.observed_permission_digest),
            ("expected_remote_version", self.expected_remote_version),
            ("observed_remote_version", self.observed_remote_version),
            ("preflight_evidence_digest", self.preflight_evidence_digest),
        ):
            _sha256(value, name)
        if self.approved_cross_boundary_id is not None:
            _safe_id(self.approved_cross_boundary_id, "approved_cross_boundary_id")
        _timestamp(self.observed_at, "observed_at")


@dataclass(frozen=True, slots=True)
class ReviewedPublication:
    publication_id: str
    operation_id: str
    kind: PublicationKind
    source_repository_id: str
    destination_repository_id: str
    exact_refspec: str
    commit_sha: str
    tree_sha: str
    lease_id: str
    profile_id: str
    actor_class: ActorClass
    actor_id: str
    installation_id: str | None
    topology_digest: str
    capability_digest: str
    permission_digest: str
    remote_version: str
    preflight_evidence_digest: str
    evidence_digests: tuple[str, ...]
    cross_boundary_approval_id: str | None
    reviewed_at: str
    review_digest: str

    def __post_init__(self) -> None:
        _safe_id(self.publication_id, "publication_id")
        _safe_id(self.operation_id, "operation_id")
        if not isinstance(self.kind, PublicationKind):
            raise ValueError("kind must be a PublicationKind")
        _safe_id(self.source_repository_id, "source_repository_id")
        _safe_id(self.destination_repository_id, "destination_repository_id")
        _bounded_text(self.exact_refspec, "exact_refspec")
        _sha40(self.commit_sha, "commit_sha")
        _sha40(self.tree_sha, "tree_sha")
        _safe_id(self.lease_id, "lease_id")
        _safe_id(self.profile_id, "profile_id")
        if not isinstance(self.actor_class, ActorClass):
            raise ValueError("actor_class must be an ActorClass")
        _safe_id(self.actor_id, "actor_id")
        if self.installation_id is not None:
            _safe_id(self.installation_id, "installation_id")
        for name, value in (
            ("topology_digest", self.topology_digest),
            ("capability_digest", self.capability_digest),
            ("permission_digest", self.permission_digest),
            ("remote_version", self.remote_version),
            ("preflight_evidence_digest", self.preflight_evidence_digest),
            ("review_digest", self.review_digest),
        ):
            _sha256(value, name)
        if not isinstance(self.evidence_digests, tuple) or not self.evidence_digests:
            raise ValueError("evidence_digests must be a non-empty tuple")
        for value in self.evidence_digests:
            _sha256(value, "evidence_digest")
        if self.cross_boundary_approval_id is not None:
            _safe_id(self.cross_boundary_approval_id, "cross_boundary_approval_id")
        _timestamp(self.reviewed_at, "reviewed_at")

    def payload(self) -> dict[str, object]:
        return {
            "publication_id": self.publication_id,
            "operation_id": self.operation_id,
            "kind": self.kind.value,
            "source_repository_id": self.source_repository_id,
            "destination_repository_id": self.destination_repository_id,
            "exact_refspec": self.exact_refspec,
            "commit_sha": self.commit_sha,
            "tree_sha": self.tree_sha,
            "lease_id": self.lease_id,
            "profile_id": self.profile_id,
            "actor_class": self.actor_class.value,
            "actor_id": self.actor_id,
            "installation_id": self.installation_id,
            "topology_digest": self.topology_digest,
            "capability_digest": self.capability_digest,
            "permission_digest": self.permission_digest,
            "remote_version": self.remote_version,
            "preflight_evidence_digest": self.preflight_evidence_digest,
            "evidence_digests": list(self.evidence_digests),
            "cross_boundary_approval_id": self.cross_boundary_approval_id,
            "reviewed_at": self.reviewed_at,
            "review_digest": self.review_digest,
        }


def _required_surface(kind: PublicationKind) -> IdentitySurface:
    if kind is PublicationKind.GIT_PUSH:
        return IdentitySurface.GIT_PUSH
    if kind is PublicationKind.PULL_REQUEST:
        return IdentitySurface.PULL_REQUEST
    return IdentitySurface.RELEASE


def _source_and_destination(
    kind: PublicationKind,
    topology: RemoteTopology,
) -> tuple[RepositoryEndpoint, RepositoryEndpoint]:
    if kind is PublicationKind.PULL_REQUEST:
        return topology.head, topology.base
    return topology.fetch, topology.push


def _require_topology(intent: PublicationIntent, evidence: PublicationEvidence) -> None:
    preflight = evidence.preflight_topology
    observed = evidence.observed_topology
    for field, value in (
        ("intent.source_ref", intent.source_ref),
        ("intent.destination_ref", intent.destination_ref),
        ("observed.source_ref", observed.source_ref),
        ("observed.destination_ref", observed.destination_ref),
    ):
        _require_exact_managed_ref(value, field)
    if preflight.rewrite_digest != observed.rewrite_digest:
        raise _publication_error(
            ErrorCode.REMOTE_REWRITE_DETECTED,
            "Remote URL rewrite configuration changed after publication preflight.",
        )
    if preflight.review_payload() != observed.review_payload():
        raise _publication_error(
            ErrorCode.PUBLICATION_TARGET_MISMATCH,
            "Repository publication topology changed after preflight.",
        )
    if len(observed.push.url_digests) != 1:
        raise _publication_error(
            ErrorCode.PUBLICATION_TARGET_MISMATCH,
            "Managed publication requires exactly one reviewed push URL.",
        )
    source, destination = _source_and_destination(intent.kind, observed)
    if (
        intent.remote_name != observed.remote_name
        or intent.source_repository_id != source.repository_id
        or intent.destination_repository_id != destination.repository_id
        or intent.source_ref != observed.source_ref
        or intent.destination_ref != observed.destination_ref
    ):
        raise _publication_error(
            ErrorCode.PUBLICATION_TARGET_MISMATCH,
            "Publication intent does not match the observed repository target and exact refs.",
        )
    if intent.kind is PublicationKind.PULL_REQUEST and (
        intent.base_ref != observed.base.exact_ref or intent.head_ref != observed.head.exact_ref
    ):
        raise _publication_error(
            ErrorCode.PUBLICATION_TARGET_MISMATCH,
            "Pull request base or head ref does not match the observed fork topology.",
        )


def _require_lease(intent: PublicationIntent, evidence: PublicationEvidence) -> None:
    lease = evidence.lease
    if lease.state is AuthLeaseState.EXPIRED or _timestamp(
        lease.expires_at, "lease.expires_at"
    ) <= _timestamp(evidence.observed_at, "observed_at"):
        raise _publication_error(
            ErrorCode.CREDENTIAL_EXPIRED,
            "The pinned publication auth lease expired before the write boundary.",
        )
    if lease.state is not AuthLeaseState.ACTIVE:
        raise _publication_error(
            ErrorCode.CREDENTIAL_REVOKED,
            "The pinned publication auth lease is not active.",
        )
    expected_target_id = intent.destination_repository_id
    if (
        lease.profile_id != evidence.profile_id
        or lease.repository_id != intent.destination_repository_id
        or lease.target_kind is not AuthTargetKind.REPOSITORY
        or lease.target_id != expected_target_id
        or lease.actor_id != evidence.actor_id
    ):
        raise _publication_error(
            ErrorCode.OPERATION_IDENTITY_MISMATCH,
            "The auth lease does not match the reviewed operation actor and publication target.",
        )
    metadata = dict(lease.provider_metadata)
    if (
        evidence.installation_id is not None
        and metadata.get("installation_id") != evidence.installation_id
    ):
        raise _publication_error(
            ErrorCode.OPERATION_IDENTITY_MISMATCH,
            "The auth lease installation does not match the reviewed publication actor.",
        )


def _require_surfaces(intent: PublicationIntent, evidence: PublicationEvidence) -> None:
    api_actor = any(
        item.surface is IdentitySurface.GITHUB_API
        and item.evidence_kind is IdentityEvidenceKind.VERIFIED_ACTOR
        and item.repository_id == intent.destination_repository_id
        and item.profile_id == evidence.profile_id
        and item.actor_id == evidence.actor_id
        for item in evidence.identity_surfaces
    )
    effect_surface = _required_surface(intent.kind)
    target_proof = any(
        item.surface is effect_surface
        and item.repository_id == intent.destination_repository_id
        and item.profile_id == evidence.profile_id
        for item in evidence.identity_surfaces
    )
    if not api_actor or not target_proof:
        raise _publication_error(
            ErrorCode.EVIDENCE_INVALID,
            "Publication requires matching API actor and target transport/effect evidence.",
        )


def _require_versions(intent: PublicationIntent, evidence: PublicationEvidence) -> None:
    if intent.expected_commit_sha != evidence.observed_commit_sha:
        raise _publication_error(
            ErrorCode.STALE_STATE,
            "The publication source commit changed after intent review.",
        )
    if intent.expected_tree_sha is None or intent.expected_tree_sha != evidence.observed_tree_sha:
        raise _publication_error(
            ErrorCode.STALE_STATE,
            "The publication source tree changed after intent review.",
        )
    if evidence.expected_capability_digest != evidence.observed_capability_digest:
        raise _publication_error(
            ErrorCode.CREDENTIAL_CAPABILITY_DENIED,
            "Publication capability evidence changed before effect.",
        )
    if evidence.expected_permission_digest != evidence.observed_permission_digest:
        raise _publication_error(
            ErrorCode.GITHUB_API_PERMISSION_DENIED,
            "GitHub permission evidence changed before publication.",
        )
    if evidence.expected_remote_version != evidence.observed_remote_version:
        raise _publication_error(
            ErrorCode.PR_REMOTE_VERSION_STALE,
            "The publication target remote version changed before effect.",
        )


def _require_boundary(intent: PublicationIntent, evidence: PublicationEvidence) -> None:
    source, destination = _source_and_destination(intent.kind, evidence.observed_topology)
    crosses_boundary = (
        source.repository_id != destination.repository_id
        or source.boundary_id != destination.boundary_id
    )
    if crosses_boundary and (
        intent.cross_boundary_approval_id is None
        or evidence.approved_cross_boundary_id != intent.cross_boundary_approval_id
    ):
        raise _publication_error(
            ErrorCode.CROSS_BOUNDARY_PUBLICATION_DENIED,
            "Cross-boundary publication requires the same exact reviewed approval identity.",
        )


def review_publication(
    intent: PublicationIntent,
    evidence: PublicationEvidence,
) -> ReviewedPublication:
    """Review one immutable publication intent immediately before its effect boundary."""

    if not isinstance(intent, PublicationIntent) or not isinstance(evidence, PublicationEvidence):
        raise TypeError("review_publication requires PublicationIntent and PublicationEvidence")
    if intent.operation_id != evidence.operation_id:
        raise _publication_error(
            ErrorCode.OPERATION_IDENTITY_MISMATCH,
            "Publication intent and evidence belong to different operations.",
        )
    _require_topology(intent, evidence)
    _require_lease(intent, evidence)
    _require_surfaces(intent, evidence)
    _require_versions(intent, evidence)
    _require_boundary(intent, evidence)

    evidence_digests = tuple(sorted(item.evidence_digest for item in evidence.identity_surfaces))
    review_payload = {
        "intent": intent.payload(),
        "actor_class": evidence.actor_class.value,
        "actor_id": evidence.actor_id,
        "installation_id": evidence.installation_id,
        "lease_id": evidence.lease.lease_id,
        "profile_id": evidence.profile_id,
        "tree_sha": evidence.observed_tree_sha,
        "topology_digest": evidence.observed_topology.topology_digest,
        "capability_digest": evidence.observed_capability_digest,
        "permission_digest": evidence.observed_permission_digest,
        "remote_version": evidence.observed_remote_version,
        "preflight_evidence_digest": evidence.preflight_evidence_digest,
        "evidence_digests": evidence_digests,
        "approved_cross_boundary_id": evidence.approved_cross_boundary_id,
        "reviewed_at": evidence.observed_at,
    }
    return ReviewedPublication(
        publication_id=intent.publication_id,
        operation_id=intent.operation_id,
        kind=intent.kind,
        source_repository_id=intent.source_repository_id,
        destination_repository_id=intent.destination_repository_id,
        exact_refspec=intent.exact_refspec,
        commit_sha=intent.expected_commit_sha,
        tree_sha=evidence.observed_tree_sha,
        lease_id=evidence.lease.lease_id,
        profile_id=evidence.profile_id,
        actor_class=evidence.actor_class,
        actor_id=evidence.actor_id,
        installation_id=evidence.installation_id,
        topology_digest=evidence.observed_topology.topology_digest,
        capability_digest=evidence.observed_capability_digest,
        permission_digest=evidence.observed_permission_digest,
        remote_version=evidence.observed_remote_version,
        preflight_evidence_digest=evidence.preflight_evidence_digest,
        evidence_digests=evidence_digests,
        cross_boundary_approval_id=intent.cross_boundary_approval_id,
        reviewed_at=evidence.observed_at,
        review_digest=_digest(review_payload),
    )


__all__ = [
    "PublicationEvidence",
    "RemoteTopology",
    "RepositoryEndpoint",
    "ReviewedPublication",
    "review_publication",
]
