from dataclasses import dataclass

from ...config import RepositoryConfig
from ..context import ApplicationContext


@dataclass(frozen=True, slots=True)
class RecentCommitsCommand:
    repo_id: str
    limit: int = 20


@dataclass(frozen=True, slots=True)
class RecentCommitsResult:
    repo_id: str
    commits: list[dict[str, str]]


class RecentCommitsReader:
    def __init__(self, ctx: ApplicationContext):
        self.ctx = ctx

    def execute(self, c: RecentCommitsCommand) -> RecentCommitsResult:
        repo = self.ctx.repo(c.repo_id)
        limit = max(1, min(c.limit, 100))
        return self.ctx.audited(
            "repo_recent_commits",
            {"repo_id": c.repo_id, "limit": limit},
            lambda: self._build(c.repo_id, repo, limit),
        )

    def compute(self, c: RecentCommitsCommand) -> RecentCommitsResult:
        """Pure application logic with no audit event, for embedding in a larger audited bundle."""
        repo = self.ctx.repo(c.repo_id)
        limit = max(1, min(c.limit, 100))
        return self._build(c.repo_id, repo, limit)

    def _build(self, repo_id: str, repo: RepositoryConfig, limit: int) -> RecentCommitsResult:
        return RecentCommitsResult(repo_id, self.ctx.git.recent_commits(repo.path, limit))
