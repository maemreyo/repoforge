from dataclasses import dataclass
from typing import Any

from ...domain.errors import WorkspaceError
from ...domain.publishing import validate_commit_message
from ..context import ApplicationContext


@dataclass(frozen=True, slots=True)
class WorkspaceCommitCommand:
    workspace_id: str
    message: str


@dataclass(frozen=True, slots=True)
class WorkspaceCommitResult:
    workspace_id: str
    branch: str
    commit: str
    head_sha: str
    verified_profile: str | None
    change_metrics: dict[str, Any]


class WorkspaceCommitter:
    def __init__(self, ctx: ApplicationContext):
        self.ctx = ctx

    def execute(self, c: WorkspaceCommitCommand) -> WorkspaceCommitResult:
        _, repo, path = self.ctx.workspace(c.workspace_id)
        message = validate_commit_message(c.message)

        def op() -> WorkspaceCommitResult:
            with self.ctx.locks.lock(c.workspace_id):
                fresh = self.ctx.store.load(c.workspace_id)
                self.ctx.git.changed_paths(path, repo)
                metrics = self.ctx.git.enforce_change_budget(path, repo)
                dirty = bool(self.ctx.git.status_porcelain(path).strip())
                current_head = self.ctx.git.head_sha(path)
                controlled_refresh = fresh.metadata.get("refresh_commit_sha") == current_head
                if not dirty and not controlled_refresh:
                    raise WorkspaceError("There are no changes to commit")
                if repo.require_verification_before_commit:
                    if not fresh.last_verification:
                        raise WorkspaceError(
                            "A successful verification profile is required before commit"
                        )
                    if self.ctx.git.fingerprint(path) != fresh.last_verification.fingerprint:
                        raise WorkspaceError(
                            "Working tree changed after verification; run a verification profile again"
                        )
                profile = fresh.last_verification.profile if fresh.last_verification else None
                completed = (
                    fresh.last_verification.completed_at if fresh.last_verification else None
                )
                if controlled_refresh and not dirty:
                    head = current_head
                    show = self.ctx.git.commit_summary(path)
                else:
                    head, show = self.ctx.git.commit(path, message)
                if repo.require_verification_before_commit:
                    fresh.metadata.update(
                        {
                            "verified_commit_sha": head,
                            "verification_profile": profile,
                            "verification_completed_at": completed,
                        }
                    )
                fresh.metadata.pop("refresh_commit_sha", None)
                fresh.last_verification = None
                try:
                    self.ctx.store.save(fresh)
                except Exception as exc:
                    raise WorkspaceError(
                        f"Commit {head} succeeded but workspace registry update failed; do not push until state is repaired"
                    ) from exc
                return WorkspaceCommitResult(
                    c.workspace_id, fresh.branch, show, head, profile, metrics
                )

        return self.ctx.audited(
            "workspace_commit",
            {"workspace_id": c.workspace_id, "message_length": len(message)},
            op,
        )
