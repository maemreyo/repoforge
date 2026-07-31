from dataclasses import dataclass, field

from ...domain.auth_profile import AuthProfileSelector
from ...domain.errors import CommandError, ErrorCode, WorkspaceError
from ...domain.policy import validate_branch
from ...domain.redaction import redact_text
from ...ports.workspace_publication import WorkspacePushPublication
from ..context import ApplicationContext
from .publication import exact_workspace_tree_sha, require_workspace_publication_service


@dataclass(frozen=True, slots=True)
class WorkspacePushCommand:
    workspace_id: str
    idempotency_key: str | None = None
    expected_remote_head: str | None = None
    selector: AuthProfileSelector = field(default_factory=AuthProfileSelector)


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
            if c.expected_remote_head is not None and c.expected_remote_head != remote_head_before:
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

            publications = require_workspace_publication_service(self.ctx)
            try:
                effect = publications.push(
                    WorkspacePushPublication(
                        workspace_id=c.workspace_id,
                        repo_id=fresh.repo_id,
                        cwd=path,
                        remote=fresh.remote,
                        source_ref=f"refs/heads/{fresh.branch}",
                        destination_ref=f"refs/heads/{fresh.branch}",
                        head_sha=head,
                        tree_sha=exact_workspace_tree_sha(self.ctx, path, repo, head),
                        remote_head_before=remote_head_before,
                        idempotency_key=c.idempotency_key,
                        selector=c.selector,
                    )
                )
            except CommandError as exc:
                raw_stderr = exc.details.get("stderr_excerpt")
                rendered = redact_text(
                    raw_stderr if isinstance(raw_stderr, str) and raw_stderr else str(exc)
                )
                rejected = any(
                    marker in rendered.lower()
                    for marker in ("non-fast-forward", "fetch first", "rejected", "stale info")
                )
                remote_head_after: str | None = None
                remote_head_observed = False
                try:
                    remote_head_after = self.ctx.git.remote_branch_sha(
                        path,
                        fresh.remote,
                        fresh.branch,
                        self.ctx.config.server.verification_timeout_seconds,
                    )
                    remote_head_observed = True
                except Exception:
                    pass
                exc.details.update(
                    {
                        "remote_head_before": remote_head_before,
                        "remote_head_after": remote_head_after,
                        "remote_head_after_observed": remote_head_observed,
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
            if effect.remote_head_before != remote_head_before:
                raise WorkspaceError(
                    "Publication result does not match the reviewed remote-head precondition",
                    code=ErrorCode.STATE_INVALID,
                    retryable=False,
                    details={
                        "expected_remote_head": remote_head_before,
                        "effect_remote_head_before": effect.remote_head_before,
                    },
                )
            if effect.head_sha != head or effect.remote_head_after != head:
                raise WorkspaceError(
                    "Publication completed but the exact destination ref does not match workspace HEAD",
                    code=ErrorCode.STATE_PERSISTENCE_FAILED,
                    retryable=True,
                    details={
                        "expected_remote_head": head,
                        "actual_remote_head": effect.remote_head_after,
                        "publication_id": effect.publication_id,
                        "operation_id": effect.operation_id,
                        "receipt_id": effect.receipt_id,
                    },
                )
            authoritative_result = WorkspacePushResult(
                summary=f"Pushed {head} to {fresh.remote}/{fresh.branch}",
                workspace_id=c.workspace_id,
                branch=fresh.branch,
                remote=fresh.remote,
                head_sha=head,
                remote_head_before=remote_head_before,
                remote_head_after=effect.remote_head_after,
                pushed=effect.pushed,
                retryable_rejection=False,
                output=redact_text(effect.output),
            )
            fresh.metadata["last_pushed_sha"] = head
            try:
                self.ctx.store.save(fresh)
            except Exception as exc:
                raise WorkspaceError(
                    f"Push of {head} succeeded but workspace registry update failed; "
                    "retry workspace_push to reconcile state",
                    code=ErrorCode.STATE_PERSISTENCE_FAILED,
                    retryable=True,
                    details={
                        "publication_id": effect.publication_id,
                        "operation_id": effect.operation_id,
                        "receipt_id": effect.receipt_id,
                        "result_reference": effect.result_reference,
                    },
                ) from exc
            return authoritative_result
