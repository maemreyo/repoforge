from dataclasses import dataclass
from typing import Any

from ..context import ApplicationContext


@dataclass(frozen=True, slots=True)
class PullRequestReadCommand:
    repo_id: str
    pr_number: int
    fresh: bool = False


@dataclass(frozen=True, slots=True)
class PullRequestReadResult:
    payload: dict[str, Any]


class PullRequestReader:
    def __init__(self, ctx: ApplicationContext):
        self.ctx = ctx

    def execute(self, c: PullRequestReadCommand) -> PullRequestReadResult:
        return self.ctx.audited(
            "repo_pr_read",
            {"repo_id": c.repo_id, "pr_number": c.pr_number},
            lambda: self.compute(c),
        )

    def compute(self, c: PullRequestReadCommand) -> PullRequestReadResult:
        """Read one PR without creating a nested audit event."""
        if c.pr_number <= 0:
            raise ValueError("pr_number must be positive")
        repo = self.ctx.repo(c.repo_id)
        payload, cache_hit = self.ctx.github_read(
            "pr",
            c.repo_id,
            repo.path,
            c.pr_number,
            fresh=c.fresh,
            loader=lambda: self.ctx.github.pr_read(repo.path, c.pr_number),
        )
        if cache_hit:
            payload = {**payload, "cache_hit": True}
        return PullRequestReadResult(payload)
