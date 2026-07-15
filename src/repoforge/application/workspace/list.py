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
        details: dict[str, object] = {}

        def op() -> WorkspaceListResult:
            values = []
            for r in self.ctx.store.list():
                path = Path(r.path)
                exists = path.is_dir()
                dirty: bool | None = None
                if exists:
                    try:
                        dirty = bool(self.ctx.git.status_porcelain(path).strip())
                    except Exception:
                        dirty = None
                values.append(
                    {
                        "workspace_id": r.workspace_id,
                        "repo_id": r.repo_id,
                        "path": r.path,
                        "branch": r.branch,
                        "base": r.base,
                        "created_at": r.created_at,
                        "exists": exists,
                        "dirty": dirty,
                        "issue_ids": list(r.metadata.get("issue_ids", ())),
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
            details["workspace_count"] = len(values)
            return WorkspaceListResult(values)

        return self.ctx.audited("workspace_list", details, op)
