"""Reviewed source-configuration resolution for the ticket-graph use case.

``_source`` resolves the ``GitHubTicketGraphConfig`` the reader should use for
a repository (configured or a bounded ad-hoc root), and ``_expected_slug``
resolves the repository slug the reader is observing without any provider
call.  Kept in its own module so ``issue_graph`` stays under the 400-line
source budget (F-006).
"""

from __future__ import annotations

from dataclasses import replace

from ...config import GitHubTicketGraphConfig, RepositoryConfig
from ...domain.errors import ConfigError, RepoForgeError
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


__all__ = ["expected_slug", "source"]
