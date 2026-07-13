from dataclasses import dataclass
from ..context import ApplicationContext
from ...domain.errors import WorkspaceError
from ...domain.policy import validate_branch


@dataclass(frozen=True, slots=True)
class WorkspacePushCommand:
    workspace_id: str


@dataclass(frozen=True, slots=True)
class WorkspacePushResult:
    workspace_id: str
    branch: str
    remote: str
    head_sha: str
    output: str


class WorkspacePusher:
    def __init__(self, ctx: ApplicationContext):
        self.ctx = ctx

    def execute(self, c: WorkspacePushCommand) -> WorkspacePushResult:
        record, repo, path = self.ctx.workspace(c.workspace_id)
        validate_branch(record.branch, repo)

        def op() -> WorkspacePushResult:
            with self.ctx.store.lock(c.workspace_id):
                fresh = self.ctx.store.load(c.workspace_id)
                self.ctx.git.changed_paths(path, repo)
                self.ctx.git.ensure_clean(path, context="push")
                head = self.ctx.git.head_sha(path)
                if (
                    repo.require_verification_before_commit
                    and fresh.metadata.get("verified_commit_sha") != head
                ):
                    raise WorkspaceError(
                        "Current HEAD was not committed through the verified commit gate"
                    )
                result = self.ctx.git.push(
                    path,
                    fresh.remote,
                    fresh.branch,
                    self.ctx.config.server.verification_timeout_seconds,
                )
                fresh.metadata["last_pushed_sha"] = head
                try:
                    self.ctx.store.save(fresh)
                except Exception as exc:
                    raise WorkspaceError(
                        f"Push of {head} succeeded but workspace registry update failed; retry workspace_push to reconcile state"
                    ) from exc
                return WorkspacePushResult(
                    c.workspace_id, fresh.branch, fresh.remote, head, result.combined
                )

        return self.ctx.audited(
            "workspace_push",
            {
                "workspace_id": c.workspace_id,
                "branch": record.branch,
                "remote": record.remote,
            },
            op,
        )
