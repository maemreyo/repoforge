"""Reviewed source-configuration resolution for the ticket-graph use case.

``source`` resolves the ``GitHubTicketGraphConfig`` the reader should use for
a repository (configured or a bounded ad-hoc root), ``expected_slug``
resolves the repository slug the reader is observing without any provider
call, and ``resolve_observation_authority`` derives the cache authority
fingerprint from the resolved auth profile identity (F-004).  Kept in its own
module so ``issue_graph`` stays under the 400-line source budget (F-006).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace

from ...config import GitHubTicketGraphConfig, RepositoryConfig
from ...domain.errors import ConfigError, RepoForgeError
from ...domain.github_api_identity import (
    GitHubAppInstallationSpec,
    StoredGhAccountSpec,
)
from ...domain.observation import (
    GitHubObservationAuthority,
    ObservationAuthorityOrigin,
)
from ...domain.tickets import TicketGraphError, github_slug_from_remote_url
from ..context import ApplicationContext


def source(repo: RepositoryConfig, root_issue: int | None) -> GitHubTicketGraphConfig:
    configured = repo.ticket_graph
    if configured is None:
        if root_issue is None:
            raise ConfigError(
                f"Repository {repo.repo_id!r} has no GitHub ticket_graph.root_issue configured"
            )
        return GitHubTicketGraphConfig(root_issue=root_issue)
    if root_issue is None or root_issue == configured.root_issue:
        return configured
    if not isinstance(root_issue, int) or isinstance(root_issue, bool) or root_issue <= 0:
        raise TicketGraphError("root_issue must be a positive issue number")
    return replace(configured, root_issue=root_issue)


def expected_slug(
    ctx: ApplicationContext, repo: RepositoryConfig, source: GitHubTicketGraphConfig
) -> str | None:
    """The repository slug the reader should be observing, without provider calls.

    A configured ``ticket_graph.repository`` is authoritative; otherwise the
    configured remote's URL is parsed locally so a remote repointing is a cache
    miss. An unresolvable identity returns ``None`` and callers fail closed.
    """
    if source.repository is not None:
        return source.repository
    try:
        result = ctx.git.remote_url(repo.path, repo.remote)
    except RepoForgeError:
        return None
    if result.returncode != 0:
        return None
    return github_slug_from_remote_url(result.stdout.strip())


def _auth_issued_authority(
    ctx: ApplicationContext, repo: RepositoryConfig
) -> GitHubObservationAuthority | None:
    """Derive an auth-issued authority from the repository's resolved auth
    profile identity, or None when no profile is configured/resolvable.

    Only secret-free identity fields (host, login/installation, profile id)
    feed the fingerprint, so rotating the credential generation changes the
    digest without the observation code ever touching a token (F-004).
    """
    if repo.commit_identity is None:
        return None
    profile = ctx.config.auth_profiles.get(repo.commit_identity.profile_id)
    if profile is None:
        return None
    identity = profile.api_identity
    if isinstance(identity, StoredGhAccountSpec):
        canonical = json.dumps(
            {
                "kind": "stored_account",
                "host": identity.host,
                "login": identity.login,
                "profile_id": identity.profile_id,
            },
            sort_keys=True,
            ensure_ascii=False,
        )
        principal = identity.login
    elif isinstance(identity, GitHubAppInstallationSpec):
        canonical = json.dumps(
            {
                "kind": "app_installation",
                "host": identity.host,
                "app_id": identity.app_id,
                "installation_id": identity.installation_id,
                "profile_id": identity.profile_id,
            },
            sort_keys=True,
            ensure_ascii=False,
        )
        principal = identity.installation_id
    else:
        return None
    return GitHubObservationAuthority(
        host=identity.host,
        profile_id=identity.profile_id,
        principal_identity=principal,
        fingerprint=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        origin=ObservationAuthorityOrigin.AUTH_ISSUED,
    )


def resolve_observation_authority(
    ctx: ApplicationContext, repo: RepositoryConfig
) -> GitHubObservationAuthority | None:
    """The authority whose fingerprint must bind any cached graph observation.

    Prefers an auth-issued fingerprint derived from the repository's resolved
    auth profile; falls back to the operator-pinned manual digest as an
    explicit legacy mode; returns ``None`` when neither is provable so the
    caller keeps the cache disabled (fail closed).
    """
    auth_issued = _auth_issued_authority(ctx, repo)
    if auth_issued is not None:
        return auth_issued
    manual = ctx.config.server.github_read_cache_authority_digest
    if manual is None:
        return None
    return GitHubObservationAuthority(
        host="github.com",
        profile_id=None,
        principal_identity=None,
        fingerprint=manual,
        origin=ObservationAuthorityOrigin.MANUAL_LEGACY,
    )


__all__ = ["expected_slug", "resolve_observation_authority", "source"]
