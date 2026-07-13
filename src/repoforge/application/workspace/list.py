from dataclasses import dataclass
from pathlib import Path

from ..context import ApplicationContext


@dataclass(frozen=True, slots=True)
class WorkspaceListCommand:
    pass


@dataclass(frozen=True, slots=True)
class WorkspaceListResult:
    workspaces: list[dict[str, object]]


class WorkspaceLister:
    def __init__(self, ctx: ApplicationContext):
        self.ctx = ctx

    def execute(self, c: WorkspaceListCommand) -> WorkspaceListResult:
        values = []
        for r in self.ctx.store.list():
            values.append(
                {
                    "workspace_id": r.workspace_id,
                    "repo_id": r.repo_id,
                    "path": r.path,
                    "branch": r.branch,
                    "base": r.base,
                    "created_at": r.created_at,
                    "exists": Path(r.path).is_dir(),
                    "lifecycle": (
                        "active"
                        if r.repo_id in self.ctx.config.repositories
                        else "orphaned_read_only"
                    ),
                    "last_verification": {
                        "profile": r.last_verification.profile,
                        "completed_at": r.last_verification.completed_at,
                    }
                    if r.last_verification
                    else None,
                }
            )
        return WorkspaceListResult(values)
