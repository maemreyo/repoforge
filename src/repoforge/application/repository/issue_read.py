from dataclasses import dataclass
from typing import Any
from ..context import ApplicationContext


@dataclass(frozen=True, slots=True)
class IssueReadCommand:
    repo_id: str
    issue_number: int


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
        return self.ctx.audited(
            "repo_issue_read",
            {"repo_id": c.repo_id, "issue_number": c.issue_number},
            lambda: IssueReadResult(
                self.ctx.github.issue_read(repo.path, c.issue_number)
            ),
        )
