from dataclasses import asdict, dataclass

from ...domain.errors import WorkspaceError
from ..context import ApplicationContext
from ..idempotency import IdempotencyEffectBoundary
from ..outcome_receipts import execute_with_outcome_receipt
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
        audit_details = {
            "workspace_id": c.workspace_id,
            "delete_local_branch": c.delete_local_branch,
        }
        boundary = IdempotencyEffectBoundary()

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
                # An adopted branch predates this workspace and belongs to the operator, so
                # removing the workspace must never delete it: that would destroy work this
                # workspace did not create, and unlike everything else here it is not
                # recoverable from the registry.
                delete_branch = c.delete_local_branch and not record.metadata.get("adopted_branch")
                if c.delete_local_branch and not delete_branch:
                    raise WorkspaceError(
                        f"ADOPTED_BRANCH_NOT_DELETABLE: {record.branch!r} was adopted, not "
                        "created by this workspace, so removal will not delete it",
                        safe_next_action=(
                            "Re-run workspace_remove without delete_local_branch to release "
                            "the worktree and keep the branch, or delete the branch yourself "
                            "once you are done with it."
                        ),
                        unchanged_state=(
                            "The workspace, its worktree, the branch, and the registry were "
                            "not modified.",
                        ),
                    )
                # Declared here, not before the checks above: everything up to this point
                # is a pure refusal that leaves the worktree, branch and registry exactly
                # as they were, and reporting those as an effect of unknown outcome would
                # tell an operator to go inspect state that was never touched.
                boundary.begin()
                deleted = self.ctx.git.remove_worktree(repo, path, record.branch, delete_branch)
                authoritative_result = WorkspaceRemoveResult(c.workspace_id, True, deleted)
                boundary.record_result(authoritative_result)
                self.ctx.store.delete(c.workspace_id)
                return authoritative_result

        return execute_with_outcome_receipt(
            self.ctx,
            "workspace_remove",
            asdict(c),
            op,
            details=audit_details,
            serialize=asdict,
            effect_boundary=boundary,
        )
