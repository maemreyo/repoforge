import contextlib
from dataclasses import asdict, dataclass
from typing import Any

from ...config import RepositoryConfig
from ...domain.command_source import dirty_command_source_paths
from ...domain.errors import CommandError, ErrorCode, WorkspaceError
from ...domain.publishing import validate_commit_message
from ..context import ApplicationContext
from ..execution.requests import profile_execution_request
from ..idempotency import IdempotencyEffectBoundary
from ..outcome_receipts import execute_with_outcome_receipt


@dataclass(frozen=True, slots=True)
class WorkspaceCommitCommand:
    workspace_id: str
    message: str
    expected_head_sha: str | None = None
    expected_fingerprint: str | None = None


@dataclass(frozen=True, slots=True)
class WorkspaceCommitResult:
    summary: str
    workspace_id: str
    branch: str
    commit: str
    previous_head_sha: str
    head_sha: str
    committed: bool
    verified_profile: str | None
    verification_fingerprint: str
    change_metrics: dict[str, Any]
    command_source_paths_committed: list[str]


def _all_command_source_paths(repo: RepositoryConfig) -> tuple[str, ...]:
    """The union of every enrolled profile's command-source paths for this repository."""
    union: set[str] = set()
    for profile in repo.profiles.values():
        union.update(profile.command_source_paths)
    return tuple(sorted(union))


class WorkspaceCommitter:
    def __init__(self, ctx: ApplicationContext):
        self.ctx = ctx

    def execute(self, c: WorkspaceCommitCommand) -> WorkspaceCommitResult:
        _, repo, path = self.ctx.workspace(c.workspace_id)
        message = validate_commit_message(c.message)
        audit_details: dict[str, object] = {
            "workspace_id": c.workspace_id,
            "message_length": len(message),
        }
        boundary = IdempotencyEffectBoundary()

        def op() -> WorkspaceCommitResult:
            with self.ctx.locks.lock(c.workspace_id):
                fresh = self.ctx.store.load(c.workspace_id)
                committed_paths = self.ctx.git.changed_paths(path, repo)
                command_source_union = _all_command_source_paths(repo)
                command_source_paths_committed = list(
                    dirty_command_source_paths(frozenset(committed_paths), command_source_union)
                )
                if command_source_paths_committed:
                    audit_details["command_source_paths_committed"] = command_source_paths_committed
                metrics = self.ctx.git.enforce_change_budget(path, repo)
                dirty = bool(self.ctx.git.status_porcelain(path).strip())
                current_head = self.ctx.git.head_sha(path)
                current_fingerprint = self.ctx.git.fingerprint(path)
                if c.expected_head_sha is not None and c.expected_head_sha != current_head:
                    raise WorkspaceError(
                        "STALE_STATE: workspace HEAD changed before commit",
                        code=ErrorCode.STALE_STATE,
                        retryable=True,
                        details={
                            "expected_head_sha": c.expected_head_sha,
                            "actual_head_sha": current_head,
                        },
                    )
                if (
                    c.expected_fingerprint is not None
                    and c.expected_fingerprint != current_fingerprint
                ):
                    raise WorkspaceError(
                        "STALE_STATE: workspace fingerprint changed before commit",
                        code=ErrorCode.STALE_STATE,
                        retryable=True,
                        details={
                            "expected_fingerprint": c.expected_fingerprint,
                            "actual_fingerprint": current_fingerprint,
                        },
                    )
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
                    verified_profile = repo.profiles.get(fresh.last_verification.profile)
                    if verified_profile is None:
                        raise WorkspaceError(
                            "Verified profile is no longer enrolled",
                            code=ErrorCode.EXECUTION_ENVIRONMENT_DRIFT,
                        )
                    request = profile_execution_request(
                        workspace_id=c.workspace_id,
                        workspace_root=path,
                        command_cwd=(path / (verified_profile.working_directory or ".")).resolve(
                            strict=False
                        ),
                        commands=tuple(step.command for step in verified_profile.steps),
                        working_directory_policy=verified_profile.working_directory or ".",
                        timeout_seconds=(
                            verified_profile.timeout_seconds
                            or self.ctx.config.server.verification_timeout_seconds
                        ),
                        output_limit=self.ctx.config.server.max_tool_output_chars,
                    )
                    inspection = self.ctx.execution.inspect(request)
                    drift_dimensions: list[str] = []
                    if (
                        inspection.identity.identity_hash
                        != fresh.last_verification.environment_identity_hash
                    ):
                        drift_dimensions.append("environment_identity")
                    if (
                        inspection.requested_policy_hash
                        != fresh.last_verification.requested_policy_hash
                    ):
                        drift_dimensions.append("requested_policy")
                    if (
                        inspection.effective_policy_hash
                        != fresh.last_verification.effective_policy_hash
                    ):
                        drift_dimensions.append("effective_policy")
                    if drift_dimensions:
                        raise WorkspaceError(
                            "Execution environment changed after verification; run the verification profile again",
                            code=ErrorCode.EXECUTION_ENVIRONMENT_DRIFT,
                            details={"drift_dimensions": drift_dimensions},
                            unchanged_state=("No commit was created.",),
                        )
                profile = fresh.last_verification.profile if fresh.last_verification else None
                completed = (
                    fresh.last_verification.completed_at if fresh.last_verification else None
                )
                before_commit_fingerprint = current_fingerprint
                verification_fingerprint = (
                    fresh.last_verification.fingerprint
                    if fresh.last_verification is not None
                    else before_commit_fingerprint
                )
                committed = not (controlled_refresh and not dirty)
                try:
                    if not committed:
                        head = current_head
                        show = self.ctx.git.commit_summary(path)
                    else:
                        boundary.begin()
                        head, show = self.ctx.git.commit(path, message)
                except CommandError as exc:
                    after_paths: list[str] = []
                    after_fingerprint: str | None = None
                    with contextlib.suppress(Exception):
                        after_paths = sorted(self.ctx.git.changed_paths(path, repo))
                    with contextlib.suppress(Exception):
                        after_fingerprint = self.ctx.git.fingerprint(path)
                    tree_mutated = bool(
                        after_fingerprint is not None
                        and after_fingerprint != before_commit_fingerprint
                    )
                    if boundary.started and not tree_mutated:
                        boundary.rollback()
                    verification_invalidated = False
                    if tree_mutated and fresh.last_verification is not None:
                        fresh.last_verification = None
                        self.ctx.store.save(fresh)
                        verification_invalidated = True
                    exc.details.setdefault("commit_stage", "git_commit")
                    exc.details.update(
                        {
                            "changed_paths_after_failure": after_paths,
                            "tree_mutated_during_commit": tree_mutated,
                            "verification_invalidated": verification_invalidated,
                        }
                    )
                    exc.safe_next_action = (
                        "Review workspace_diff and the reported hook output. If hooks changed files, "
                        "review those changes, rerun the verification profile on the exact tree, "
                        "then retry workspace_commit."
                    )
                    audit_details.update(
                        {
                            "commit_stage": exc.details.get("commit_stage"),
                            "tree_mutated_during_commit": tree_mutated,
                            "verification_invalidated": verification_invalidated,
                            "changed_path_count_after_failure": len(after_paths),
                        }
                    )
                    raise
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
                if not boundary.started:
                    boundary.begin()
                authoritative_result = WorkspaceCommitResult(
                    summary=(
                        f"Committed workspace changes at {head}"
                        if committed
                        else f"Adopted controlled refresh commit {head}"
                    ),
                    workspace_id=c.workspace_id,
                    branch=fresh.branch,
                    commit=show,
                    previous_head_sha=current_head,
                    head_sha=head,
                    committed=committed,
                    verified_profile=profile,
                    verification_fingerprint=verification_fingerprint,
                    change_metrics=metrics,
                    command_source_paths_committed=command_source_paths_committed,
                )
                boundary.record_result(authoritative_result)
                try:
                    self.ctx.store.save(fresh)
                except Exception as exc:
                    raise WorkspaceError(
                        f"Commit {head} succeeded but workspace registry update failed; do not push until state is repaired"
                    ) from exc
                return authoritative_result

        return execute_with_outcome_receipt(
            self.ctx,
            "workspace_commit",
            asdict(c),
            op,
            details=audit_details,
            serialize=asdict,
            effect_boundary=boundary,
        )
