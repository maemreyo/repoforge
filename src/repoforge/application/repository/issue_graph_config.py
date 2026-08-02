"""Reviewed source-configuration resolution for the ticket-graph use case.

``source`` resolves the ``GitHubTicketGraphConfig`` the reader should use for
a repository (configured or a bounded ad-hoc root), ``expected_slug``
resolves the repository slug the reader is observing without any provider
call, and ``resolve_observation_authority`` resolves the cache authority that
must bind any cached graph observation (F-004).  Kept in its own module so
``issue_graph`` stays under the 400-line source budget (F-006).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace

from ...config import GitHubTicketGraphConfig, RepositoryConfig
from ...domain.errors import ConfigError, RepoForgeError
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


def build_auth_issued_authority(
    *,
    host: str,
    profile_id: str,
    principal_identity: str,
    credential_generation: str,
    authorization_scope_digest: str,
) -> GitHubObservationAuthority:
    """Build an ``AUTH_ISSUED`` authority from the trust boundary's live
    credential generation (F-004).

    ``credential_generation`` must come from the auth lease/material that owns
    the credential lifecycle — it is never derived from static configuration.
    The fingerprint covers the host, principal, generation, and authorization
    scope, so rotating the generation or the scope rotates the fingerprint and
    every prior cache entry becomes a miss.
    """
    canonical = json.dumps(
        {
            "kind": "auth_issued",
            "host": host,
            "principal_identity": principal_identity,
            "credential_generation": credential_generation,
            "authorization_scope_digest": authorization_scope_digest,
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return GitHubObservationAuthority(
        host=host,
        profile_id=profile_id,
        principal_identity=principal_identity,
        credential_generation=credential_generation,
        authorization_scope_digest=authorization_scope_digest,
        fingerprint=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        origin=ObservationAuthorityOrigin.AUTH_ISSUED,
    )


def resolve_observation_authority(
    ctx: ApplicationContext, repo: RepositoryConfig
) -> GitHubObservationAuthority | None:
    """The authority whose fingerprint must bind any cached graph observation.

    Prefers an authority issued by the trust boundary that owns the credential
    lifecycle (the auth broker / lease) when one is wired on the context;
    falls back to the operator-pinned manual digest as an explicit legacy mode;
    returns ``None`` when neither is provable so the caller keeps the cache
    disabled (fail closed).  A static profile hash is never labeled
    ``AUTH_ISSUED`` — it does not prove the current credential generation
    (F-004).
    """
    issued = (
        ctx.observation_authority_provider(repo) if ctx.observation_authority_provider else None
    )
    if issued is not None:
        return issued
    manual = ctx.config.server.github_read_cache_authority_digest
    if manual is None:
        return None
    return GitHubObservationAuthority(
        host="github.com",
        profile_id=None,
        principal_identity=None,
        credential_generation=None,
        authorization_scope_digest=None,
        fingerprint=manual,
        origin=ObservationAuthorityOrigin.MANUAL_LEGACY,
    )


__all__ = [
    "build_auth_issued_authority",
    "expected_slug",
    "resolve_observation_authority",
    "source",
]
