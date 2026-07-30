"""Narrow read-only boundaries for observing each repository identity surface separately.

Each protocol returns the evidence type that surface already owns, so success on one surface
can never be read as success on another: proving a transport reaches a host says nothing about
which API actor a token belongs to, and an unsigned commit attestation never names a signer.
An inspector that is simply not configured is absent, and the service reports that surface as
unavailable rather than falling back to ambient state.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from ..domain.commit_identity import CommitIdentityEvidence
from ..domain.git_transport_identity import GitTransportEvidence, GitTransportSpec
from ..domain.github_api_identity import (
    GitHubApiIdentityProof,
    GitHubAppInstallationSpec,
    StoredGhAccountSpec,
)
from ..domain.publication import RemoteTopology


class ApiIdentityInspector(Protocol):
    def inspect(
        self, spec: StoredGhAccountSpec | GitHubAppInstallationSpec
    ) -> GitHubApiIdentityProof: ...


class TransportInspector(Protocol):
    def inspect(self, cwd: Path, spec: GitTransportSpec) -> GitTransportEvidence: ...


class CommitIdentityInspector(Protocol):
    def inspect(self, cwd: Path) -> CommitIdentityEvidence: ...


class PublicationTargetInspector(Protocol):
    def inspect(self, cwd: Path, repository_id: str) -> RemoteTopology: ...
