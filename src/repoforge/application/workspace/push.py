from dataclasses import dataclass
from typing import cast

from ...domain.errors import WorkspaceError
from ...domain.policy import validate_branch
from ...domain.redaction import redact_text
from ..context import ApplicationContext
from ..dto import to_data


@dataclass(frozen=True, slots=True)
class WorkspacePushCommand:
    workspace_id: str
    idempotency_key: str | None = None


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
            with self.ctx.locks.lock(c.workspace_id):
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
                if (
                    fresh.metadata.get("last_pushed_sha") == head
                    and self.ctx.git.upstream_sha(path) == head
                ):
                    return WorkspacePushResult(
                        c.workspace_id,
                        fresh.branch,
                        fresh.remote,
                        head,
                        "already synchronized with upstream",
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
                    c.workspace_id, fresh.branch, fresh.remote, head, redact_text(result.combined)
                )

        return cast(
            WorkspacePushResult,
            self.ctx.idempotent(
                "workspace_push",
                c.idempotency_key,
                {"workspace_id": c.workspace_id},
                op,
                details={
                    "workspace_id": c.workspace_id,
                    "branch": record.branch,
                    "remote": record.remote,
                },
                serialize=to_data,
                deserialize=lambda value: WorkspacePushResult(**value),
            ),
        )
