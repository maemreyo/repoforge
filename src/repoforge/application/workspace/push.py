from dataclasses import dataclass
from typing import cast

from ...domain.errors import CommandError, ErrorCode, WorkspaceError
from ...domain.policy import validate_branch
from ...domain.redaction import redact_text
from ..context import ApplicationContext
from ..dto import to_data
from ..idempotency import IdempotencyEffectBoundary


@dataclass(frozen=True, slots=True)
class WorkspacePushCommand:
    workspace_id: str
    idempotency_key: str | None = None
    expected_remote_head: str | None = None


@dataclass(frozen=True, slots=True)
class WorkspacePushResult:
    summary: str
    workspace_id: str
    branch: str
    remote: str
    head_sha: str
    remote_head_before: str | None
    remote_head_after: str
    pushed: bool
    retryable_rejection: bool
    output: str


class WorkspacePusher:
    def __init__(self, ctx: ApplicationContext):
        self.ctx = ctx

    def execute(self, c: WorkspacePushCommand) -> WorkspacePushResult:
        record, repo, path = self.ctx.workspace(c.workspace_id)
        validate_branch(record.branch, repo)
        boundary = IdempotencyEffectBoundary()

        def op() -> WorkspacePushResult:
            with self.ctx.locks.lock(c.workspace_id):
                fresh = self.ctx.store.load(c.workspace_id)
                self.ctx.git.changed_paths(path, repo)
                self.ctx.git.ensure_clean(path, context="push")
                head = self.ctx.git.head_sha(path)
                remote_head_before = self.ctx.git.remote_branch_sha(
                    path,
                    fresh.remote,
                    fresh.branch,
                    self.ctx.config.server.verification_timeout_seconds,
                )
                if (
                    c.expected_remote_head is not None
                    and c.expected_remote_head != remote_head_before
                ):
                    raise WorkspaceError(
                        "STALE_STATE: remote branch changed before push",
                        code=ErrorCode.STALE_STATE,
                        retryable=True,
                        details={
                            "expected_remote_head": c.expected_remote_head,
                            "actual_remote_head": remote_head_before,
                        },
                    )
                if (
                    repo.require_verification_before_commit
                    and fresh.metadata.get("verified_commit_sha") != head
                ):
                    raise WorkspaceError(
                        "Current HEAD was not committed through the verified commit gate"
                    )
                if fresh.metadata.get("last_pushed_sha") == head and remote_head_before == head:
                    return WorkspacePushResult(
                        summary="Workspace branch is already synchronized with the remote target branch",
                        workspace_id=c.workspace_id,
                        branch=fresh.branch,
                        remote=fresh.remote,
                        head_sha=head,
                        remote_head_before=head,
                        remote_head_after=head,
                        pushed=False,
                        retryable_rejection=False,
                        output="already synchronized with upstream",
                    )
                try:
                    boundary.begin()
                    result = self.ctx.git.push(
                        path,
                        fresh.remote,
                        fresh.branch,
                        self.ctx.config.server.verification_timeout_seconds,
                    )
                except CommandError as exc:
                    raw_stderr = exc.details.get("stderr_excerpt")
                    rendered = redact_text(
                        raw_stderr if isinstance(raw_stderr, str) and raw_stderr else str(exc)
                    )
                    lowered = rendered.lower()
                    rejected = any(
                        marker in lowered
                        for marker in ("non-fast-forward", "fetch first", "rejected", "stale info")
                    )
                    remote_head_after_failure: str | None = None
                    remote_head_after_observed = False
                    try:
                        remote_head_after_failure = self.ctx.git.remote_branch_sha(
                            path,
                            fresh.remote,
                            fresh.branch,
                            self.ctx.config.server.verification_timeout_seconds,
                        )
                        remote_head_after_observed = True
                    except Exception:
                        pass
                    if (
                        boundary.started
                        and remote_head_after_observed
                        and remote_head_after_failure == remote_head_before
                    ):
                        boundary.rollback()
                    elif (
                        boundary.started
                        and remote_head_after_observed
                        and remote_head_after_failure == head
                    ):
                        boundary.record_result(
                            WorkspacePushResult(
                                summary=f"Pushed {head} to {fresh.remote}/{fresh.branch}",
                                workspace_id=c.workspace_id,
                                branch=fresh.branch,
                                remote=fresh.remote,
                                head_sha=head,
                                remote_head_before=remote_head_before,
                                remote_head_after=head,
                                pushed=True,
                                retryable_rejection=False,
                                output=(
                                    "Push effect reconciled from remote state after command error: "
                                    f"{rendered}"
                                ),
                            )
                        )
                    exc.details.update(
                        {
                            "remote_head_before": remote_head_before,
                            "remote_head_after": remote_head_after_failure,
                            "remote_head_after_observed": remote_head_after_observed,
                            "retryable_rejection": False if rejected else exc.retryable,
                        }
                    )
                    if rejected:
                        exc.retryable = False
                        exc.safe_next_action = (
                            "Refresh the workspace against the latest remote branch, review the "
                            "resulting diff, then retry workspace_push without force."
                        )
                    raise
                remote_head_after = self.ctx.git.remote_branch_sha(
                    path,
                    fresh.remote,
                    fresh.branch,
                    self.ctx.config.server.verification_timeout_seconds,
                )
                if remote_head_after != head:
                    raise WorkspaceError(
                        "Push completed but the remote target branch does not match the pushed HEAD",
                        code=ErrorCode.STATE_PERSISTENCE_FAILED,
                        retryable=True,
                        details={
                            "expected_remote_head": head,
                            "actual_remote_head": remote_head_after,
                        },
                    )
                authoritative_result = WorkspacePushResult(
                    summary=f"Pushed {head} to {fresh.remote}/{fresh.branch}",
                    workspace_id=c.workspace_id,
                    branch=fresh.branch,
                    remote=fresh.remote,
                    head_sha=head,
                    remote_head_before=remote_head_before,
                    remote_head_after=remote_head_after,
                    pushed=True,
                    retryable_rejection=False,
                    output=redact_text(result.combined),
                )
                boundary.record_result(authoritative_result)
                fresh.metadata["last_pushed_sha"] = head
                try:
                    self.ctx.store.save(fresh)
                except Exception as exc:
                    raise WorkspaceError(
                        f"Push of {head} succeeded but workspace registry update failed; retry workspace_push to reconcile state"
                    ) from exc
                return authoritative_result

        return cast(
            WorkspacePushResult,
            self.ctx.idempotent(
                "workspace_push",
                c.idempotency_key,
                {
                    "workspace_id": c.workspace_id,
                    "expected_remote_head": c.expected_remote_head,
                },
                op,
                details={
                    "workspace_id": c.workspace_id,
                    "branch": record.branch,
                    "remote": record.remote,
                },
                serialize=to_data,
                deserialize=lambda value: WorkspacePushResult(**value),
                effect_boundary=boundary,
            ),
        )
