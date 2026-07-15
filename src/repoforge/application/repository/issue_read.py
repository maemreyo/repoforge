from dataclasses import dataclass
from typing import Any

from ..context import ApplicationContext


@dataclass(frozen=True, slots=True)
class IssueReadCommand:
    repo_id: str
    issue_number: int
    fresh: bool = False


@dataclass(frozen=True, slots=True)
class IssueReadResult:
    payload: dict[str, Any]


class IssueReader:
    def __init__(self, ctx: ApplicationContext):
        self.ctx = ctx

    def execute(self, c: IssueReadCommand) -> IssueReadResult:
        if c.issue_number <= 0:
            raise ValueError("issue_number must be positive")
        repo = self.ctx.repo(c.repo_id)

        def operation() -> IssueReadResult:
            payload, cache_hit = self.ctx.github_read(
                "issue",
                c.repo_id,
                c.issue_number,
                fresh=c.fresh,
                loader=lambda: self.ctx.github.issue_read(repo.path, c.issue_number),
            )
            if cache_hit:
                payload = {**payload, "cache_hit": True}
            return IssueReadResult(payload)

        return self.ctx.audited(
            "repo_issue_read",
            {"repo_id": c.repo_id, "issue_number": c.issue_number},
            operation,
        )
