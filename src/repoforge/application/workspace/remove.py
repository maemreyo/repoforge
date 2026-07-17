from dataclasses import dataclass

from ...domain.errors import WorkspaceError
from ..context import ApplicationContext
from .removal_safety import unpushed_commit_count


@dataclass(frozen=True, slots=True)
class WorkspaceRemoveCommand:
    workspace_id: str
    delete_local_branch: bool = False


@dataclass(frozen=True, slots=True)
class WorkspaceRemoveResult:
    workspace_id: str
    removed: bool
    local_branch_deleted: bool
    remote_branch_untouched: bool = True


class WorkspaceRemover:
    def __init__(self, ctx: ApplicationContext):
        self.ctx = ctx

    def execute(self, c: WorkspaceRemoveCommand) -> WorkspaceRemoveResult:
        record, repo, path = self.ctx.workspace(c.workspace_id)

        def op() -> WorkspaceRemoveResult:
            with self.ctx.locks.lock(c.workspace_id):
                try:
                    self.ctx.git.ensure_clean(path, context="workspace removal")
                except WorkspaceError as exc:
                    raise WorkspaceError(
                        str(exc),
                        safe_next_action=(
                            "Commit and push the changes, or call workspace_restore_paths to "
                            "explicitly discard them, then retry workspace_remove."
                        ),
                        unchanged_state=(
                            "The workspace, its worktree, and the workspace registry were not modified.",
                        ),
                    ) from exc
                unpushed = unpushed_commit_count(self.ctx, record, path)
                if unpushed:
                    raise WorkspaceError(
                        f"Workspace has {unpushed} commit(s) not pushed to its remote branch; "
                        "removal would discard them",
                        safe_next_action=(
                            "Call workspace_push to push the branch before removing this "
                            "workspace, or confirm those commits are intentionally disposable."
                        ),
                        unchanged_state=(
                            "The workspace, its worktree, and the workspace registry were not modified.",
                        ),
                    )
                deleted = self.ctx.git.remove_worktree(
                    repo, path, record.branch, c.delete_local_branch
                )
                self.ctx.store.delete(c.workspace_id)
                return WorkspaceRemoveResult(c.workspace_id, True, deleted)

        return self.ctx.audited(
            "workspace_remove",
            {
                "workspace_id": c.workspace_id,
                "delete_local_branch": c.delete_local_branch,
            },
            op,
        )
