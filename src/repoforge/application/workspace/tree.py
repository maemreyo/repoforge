from dataclasses import dataclass
from ..context import ApplicationContext


@dataclass(frozen=True, slots=True)
class WorkspaceTreeCommand:
    workspace_id: str
    max_entries: int = 2000


@dataclass(frozen=True, slots=True)
class WorkspaceTreeResult:
    workspace_id: str
    entries: list[str]
    truncated: bool


class WorkspaceTreeReader:
    def __init__(self, ctx: ApplicationContext):
        self.ctx = ctx

    def execute(self, c: WorkspaceTreeCommand) -> WorkspaceTreeResult:
        _, repo, path = self.ctx.workspace(c.workspace_id)
        limit = max(1, min(c.max_entries, 10000))

        def op() -> WorkspaceTreeResult:
            entries, truncated = self.ctx.git.list_files(path, repo, limit)
            return WorkspaceTreeResult(c.workspace_id, entries, truncated)

        return self.ctx.audited(
            "workspace_tree", {"workspace_id": c.workspace_id, "max_entries": limit}, op
        )
