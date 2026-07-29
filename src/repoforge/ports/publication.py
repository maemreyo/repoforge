"""Exact repository publication boundaries."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ..domain.git_transport_identity import GitTransportSpec
from ..domain.publication import RemoteTopology, ReviewedPublication
from ..domain.repository_auth_broker import ProcessAuthContext
from ..domain.repository_identity import (
    ActorClass,
    AuthLease,
    IdentitySurfaceEvidence,
    PublicationIntent,
    PublicationKind,
)

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_SHA40 = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _safe_id(value: str, field: str) -> str:
    if not isinstance(value, str) or _SAFE_ID.fullmatch(value) is None:
        raise ValueError(f"{field} must be a bounded safe identifier")
    return value


def _bounded(value: str, field: str, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or any(ord(character) < 32 for character in value)
    ):
        raise ValueError(f"{field} must be bounded non-empty text")
    return value


@dataclass(frozen=True, slots=True)
class PublicationRepositoryMetadata:
    repository_id: str
    canonical_name: str
    boundary_id: str

    def __post_init__(self) -> None:
        _safe_id(self.repository_id, "repository_id")
        _bounded(self.canonical_name, "canonical_name", 1_024)
        _safe_id(self.boundary_id, "boundary_id")


@dataclass(frozen=True, slots=True)
class PublicationAuthorization:
    profile_id: str
    actor_class: ActorClass
    actor_id: str
    installation_id: str | None
    lease: AuthLease
    identity_surfaces: tuple[IdentitySurfaceEvidence, ...]
    capability_digest: str
    permission_digest: str
    remote_version: str
    observed_at: str
    approved_cross_boundary_id: str | None

    def __post_init__(self) -> None:
        _safe_id(self.profile_id, "profile_id")
        if not isinstance(self.actor_class, ActorClass):
            raise ValueError("actor_class must be an ActorClass")
        _safe_id(self.actor_id, "actor_id")
        if self.installation_id is not None:
            _safe_id(self.installation_id, "installation_id")
        if not isinstance(self.lease, AuthLease):
            raise ValueError("lease must be an AuthLease")
        if not self.identity_surfaces or any(
            not isinstance(item, IdentitySurfaceEvidence) for item in self.identity_surfaces
        ):
            raise ValueError("identity_surfaces must contain reviewed evidence")
        for field, value in (
            ("capability_digest", self.capability_digest),
            ("permission_digest", self.permission_digest),
            ("remote_version", self.remote_version),
        ):
            if _SHA256.fullmatch(value) is None:
                raise ValueError(f"{field} must be a lowercase SHA-256")
        _bounded(self.observed_at, "observed_at", 80)
        if self.approved_cross_boundary_id is not None:
            _safe_id(self.approved_cross_boundary_id, "approved_cross_boundary_id")


@dataclass(frozen=True, slots=True)
class PullRequestPublication:
    title: str
    body: str

    def __post_init__(self) -> None:
        _bounded(self.title.strip(), "title", 256)
        if not isinstance(self.body, str) or len(self.body) > 96_000 or "\x00" in self.body:
            raise ValueError("body must be bounded text")


@dataclass(frozen=True, slots=True)
class PublicationEffect:
    publication_id: str
    kind: PublicationKind
    destination_repository_id: str
    destination_ref: str
    commit_sha: str
    external_id: str
    url: str | None
    reconciled: bool

    def __post_init__(self) -> None:
        _safe_id(self.publication_id, "publication_id")
        if not isinstance(self.kind, PublicationKind):
            raise ValueError("kind must be a PublicationKind")
        _safe_id(self.destination_repository_id, "destination_repository_id")
        _bounded(self.destination_ref, "destination_ref", 4_096)
        if _SHA40.fullmatch(self.commit_sha) is None:
            raise ValueError("commit_sha must be a full lowercase Git object ID")
        _safe_id(self.external_id, "external_id")
        if self.url is not None:
            _bounded(self.url, "url", 4_096)
        if not isinstance(self.reconciled, bool):
            raise ValueError("reconciled must be boolean")

    def safe_payload(self) -> dict[str, object]:
        return {
            "publication_id": self.publication_id,
            "kind": self.kind.value,
            "destination_repository_id": self.destination_repository_id,
            "destination_ref": self.destination_ref,
            "commit_sha": self.commit_sha,
            "external_id": self.external_id,
            "url": self.url,
            "reconciled": self.reconciled,
        }


class PublicationRepositoryResolver(Protocol):
    def resolve_url(self, url: str) -> PublicationRepositoryMetadata: ...

    def resolve_id(self, repository_id: str) -> PublicationRepositoryMetadata: ...


class PublicationAuthorizationGateway(Protocol):
    def revalidate(
        self,
        intent: PublicationIntent,
        expected: PublicationAuthorization,
    ) -> PublicationAuthorization: ...


class GitHubPublicationGateway(Protocol):
    def create_pull_request(
        self,
        *,
        cwd: Path,
        publication_id: str,
        base_repository_id: str,
        head_repository_id: str,
        base_repository: str,
        head_repository: str,
        base_ref: str,
        head_ref: str,
        expected_commit_sha: str,
        title: str,
        body: str,
        auth_context: ProcessAuthContext,
    ) -> PublicationEffect: ...

    def find_pull_request(
        self,
        *,
        cwd: Path,
        publication_id: str,
        base_repository_id: str,
        head_repository_id: str,
        base_repository: str,
        head_repository: str,
        base_ref: str,
        head_ref: str,
        expected_commit_sha: str,
        auth_context: ProcessAuthContext,
    ) -> PublicationEffect | None: ...


class PublicationGateway(Protocol):
    def inspect(self, cwd: Path, intent: PublicationIntent) -> RemoteTopology: ...

    def revalidate(
        self,
        cwd: Path,
        intent: PublicationIntent,
        preflight: RemoteTopology,
        expected_authorization: PublicationAuthorization,
    ) -> ReviewedPublication: ...

    def publish(
        self,
        cwd: Path,
        reviewed: ReviewedPublication,
        topology: RemoteTopology,
        *,
        transport_spec: GitTransportSpec,
        auth_context: ProcessAuthContext,
        pull_request: PullRequestPublication | None = None,
    ) -> PublicationEffect: ...

    def reconcile(
        self,
        cwd: Path,
        reviewed: ReviewedPublication,
        topology: RemoteTopology,
        *,
        transport_spec: GitTransportSpec,
        auth_context: ProcessAuthContext,
    ) -> PublicationEffect | None: ...


__all__ = [
    "GitHubPublicationGateway",
    "PublicationAuthorization",
    "PublicationAuthorizationGateway",
    "PublicationEffect",
    "PublicationGateway",
    "PublicationRepositoryMetadata",
    "PublicationRepositoryResolver",
    "PullRequestPublication",
]
