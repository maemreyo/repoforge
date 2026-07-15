from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ...domain.errors import ErrorCode, WorkspaceError
from ...domain.workspace import WORKSPACE_REFRESH_RECEIPTS, WorkspaceRefreshBinding
from ..context import ApplicationContext
from ..fingerprint_cache import read_fingerprint
from .base_status import collect_workspace_base_status


@dataclass(frozen=True, slots=True)
class WorkspaceRefreshPreviewCommand:
    workspace_id: str
    expected_head_sha: str
    expected_fingerprint: str


@dataclass(frozen=True, slots=True)
class WorkspaceRefreshPreviewResult:
    workspace_id: str
    preview_id: str
    configured_base: str
    workspace_base_sha: str
    target_base_sha: str
    head_sha: str
    workspace_fingerprint: str
    strategy: str
    predicted_conflict_paths: list[str]
    affected_paths: list[str]
    upstream_changed_paths: list[str]
    overlap_paths: list[str]
    invalidated_receipts: list[str]
    refreshable: bool
    blocking_reasons: list[str]
    published_state: str
    external_implications: dict[str, object]


def require_refresh_snapshot(
    ctx: ApplicationContext,
    workspace_id: str,
    path: Path,
    expected_head_sha: str,
    expected_fingerprint: str,
) -> tuple[str, str]:
    head = ctx.git.head_sha(path)
    fingerprint = read_fingerprint(ctx.fingerprint_cache, workspace_id, ctx.git, path).fingerprint
    if head != expected_head_sha or fingerprint != expected_fingerprint:
        raise WorkspaceError(
            "STALE_REFRESH_PREVIEW: workspace HEAD or fingerprint changed",
            code=ErrorCode.STALE_STATE,
            retryable=True,
            safe_next_action="Read workspace status and create a new refresh preview.",
            unchanged_state=("The workspace branch and working tree were not refreshed.",),
        )
    return head, fingerprint


def refresh_binding(
    *,
    workspace_id: str,
    configured_base: str,
    workspace_base_sha: str,
    target_base_sha: str,
    head_sha: str,
    workspace_fingerprint: str,
    conflict_paths: tuple[str, ...],
    workspace_clean: bool,
) -> WorkspaceRefreshBinding:
    return WorkspaceRefreshBinding(
        workspace_id,
        configured_base,
        workspace_base_sha,
        target_base_sha,
        head_sha,
        workspace_fingerprint,
        "merge_no_ff",
        conflict_paths,
        workspace_clean,
    )


class WorkspaceRefreshPreviewer:
    def __init__(self, ctx: ApplicationContext):
        self.ctx = ctx

    def execute(self, command: WorkspaceRefreshPreviewCommand) -> WorkspaceRefreshPreviewResult:
        _, repo, path = self.ctx.workspace(command.workspace_id)

        def operation() -> WorkspaceRefreshPreviewResult:
            with self.ctx.locks.lock(command.workspace_id):
                record = self.ctx.store.load(command.workspace_id)
                head, fingerprint = require_refresh_snapshot(
                    self.ctx,
                    command.workspace_id,
                    path,
                    command.expected_head_sha,
                    command.expected_fingerprint,
                )
                base = collect_workspace_base_status(
                    self.ctx,
                    record,
                    repo,
                    path,
                    fetch_remote=True,
                )
                require_refresh_snapshot(
                    self.ctx,
                    command.workspace_id,
                    path,
                    command.expected_head_sha,
                    command.expected_fingerprint,
                )
                if not base.remote_available or base.remote_base_sha is None:
                    raise WorkspaceError(
                        "REMOTE_BASE_UNAVAILABLE: latest remote base cannot be reviewed",
                        code=ErrorCode.COMMAND_FAILED,
                        retryable=True,
                        safe_next_action=(
                            "Restore remote connectivity and request a new refresh preview."
                        ),
                        unchanged_state=(
                            "The workspace branch and working tree were not refreshed.",
                        ),
                    )
                merge = self.ctx.git.preview_merge(path, repo, base.remote_base_sha)
                clean = not bool(self.ctx.git.status_porcelain(path).strip())
                binding = refresh_binding(
                    workspace_id=command.workspace_id,
                    configured_base=record.base,
                    workspace_base_sha=base.workspace_base_sha,
                    target_base_sha=base.remote_base_sha,
                    head_sha=head,
                    workspace_fingerprint=fingerprint,
                    conflict_paths=merge.conflict_paths,
                    workspace_clean=clean,
                )
                blocking = [] if clean else ["working_tree_not_clean"]
                affected = sorted(
                    set(base.upstream_changed_paths).union(base.workspace_changed_paths)
                )
                return WorkspaceRefreshPreviewResult(
                    command.workspace_id,
                    binding.preview_id(),
                    record.base,
                    base.workspace_base_sha,
                    base.remote_base_sha,
                    head,
                    fingerprint,
                    "merge_no_ff",
                    list(merge.conflict_paths),
                    affected,
                    base.upstream_changed_paths,
                    base.overlap_paths,
                    list(WORKSPACE_REFRESH_RECEIPTS),
                    clean,
                    blocking,
                    base.published_state,
                    {
                        "fetch_performed": True,
                        "force_push_required": False,
                        "published_branch": base.published_state != "unpublished",
                        "creates_merge_commit": not merge.already_integrated,
                    },
                )

        return self.ctx.audited(
            "workspace_refresh_preview",
            {"workspace_id": command.workspace_id},
            operation,
            mutating=False,
        )
