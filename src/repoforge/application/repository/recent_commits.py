from dataclasses import dataclass

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
            lambda: RecentCommitsResult(c.repo_id, self.ctx.git.recent_commits(repo.path, limit)),
        )
