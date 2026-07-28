"""GitHub API token issuance and live identity-proof boundaries."""

from __future__ import annotations

from typing import Protocol

from ..domain.github_api_identity import (
    GitHubApiIdentityProof,
    GitHubApiTokenGrant,
    GitHubAppInstallationSpec,
    StoredGhAccountSpec,
)
from ..domain.repository_auth_broker import EphemeralSecret


class GitHubAppJwtSigner(Protocol):
    def issue(self, *, app_id: str, issued_at: str, expires_at: str) -> EphemeralSecret: ...


class StoredGhAccountTokenSource(Protocol):
    def issue(self, spec: StoredGhAccountSpec) -> GitHubApiTokenGrant: ...


class GitHubAppInstallationTokenIssuer(Protocol):
    def issue(self, spec: GitHubAppInstallationSpec) -> GitHubApiTokenGrant: ...

    def revoke(self, grant: GitHubApiTokenGrant) -> None: ...


class GitHubApiIdentityVerifier(Protocol):
    def verify_stored_account(
        self, spec: StoredGhAccountSpec, grant: GitHubApiTokenGrant
    ) -> GitHubApiIdentityProof: ...

    def verify_app_installation(
        self, spec: GitHubAppInstallationSpec, grant: GitHubApiTokenGrant
    ) -> GitHubApiIdentityProof: ...
