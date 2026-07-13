from dataclasses import dataclass
from typing import Any
from ..context import ApplicationContext


@dataclass(frozen=True, slots=True)
class WorkspaceDiffCommand:
    workspace_id: str
    staged: bool = False


@dataclass(frozen=True, slots=True)
class WorkspaceDiffResult:
    workspace_id: str
    staged: bool
    changed_paths: list[str]
    change_metrics: dict[str, Any]
    untracked_paths: list[str]
    stat: str
    diff: str
    truncated: bool


class WorkspaceDiffReader:
    def __init__(self, ctx: ApplicationContext):
        self.ctx = ctx

    def execute(self, c: WorkspaceDiffCommand) -> WorkspaceDiffResult:
        _, repo, path = self.ctx.workspace(c.workspace_id)

        def op() -> WorkspaceDiffResult:
            d = self.ctx.git.diff(path, repo, staged=c.staged)
            return WorkspaceDiffResult(
                c.workspace_id,
                c.staged,
                d["changed_paths"],
                d["change_metrics"],
                d["untracked_paths"],
                d["stat"],
                d["diff"],
                d["truncated"],
            )

        return self.ctx.audited(
            "workspace_diff", {"workspace_id": c.workspace_id, "staged": c.staged}, op
        )
