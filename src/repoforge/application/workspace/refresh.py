from __future__ import annotations

from dataclasses import dataclass, replace

from ...domain.errors import ErrorCode, WorkspaceError
from ...domain.policy import validate_branch
from ...domain.workspace import (
    WorkspaceRefreshBinding,
    invalidate_workspace_refresh_receipts,
    refresh_preview_target,
)
from ..context import ApplicationContext
from ..fingerprint_cache import prime_fingerprint
from .base_status import collect_workspace_base_status
from .refresh_preview import refresh_binding, require_refresh_snapshot


@dataclass(frozen=True, slots=True)
class WorkspaceRefreshCommand:
    workspace_id: str
    preview_id: str
    expected_head_sha: str
    expected_fingerprint: str


@dataclass(frozen=True, slots=True)
class WorkspaceRefreshResult:
    workspace_id: str
    status: str
    configured_base: str
    workspace_base_sha: str
    previous_head_sha: str
    head_sha: str
    conflict_paths: list[str]
    invalidated_receipts: list[str]
    recovered: bool
    force_push_required: bool
    next_step: str


def _stale_refresh(message: str) -> WorkspaceError:
    return WorkspaceError(
        f"STALE_REFRESH_PREVIEW: {message}",
        code=ErrorCode.STALE_STATE,
        retryable=True,
        safe_next_action="Read workspace status and create a new refresh preview.",
        unchanged_state=("The workspace branch and working tree were not refreshed.",),
    )


class WorkspaceRefresher:
    def __init__(self, ctx: ApplicationContext):
        self.ctx = ctx

    def execute(self, command: WorkspaceRefreshCommand) -> WorkspaceRefreshResult:
        _, repo, path = self.ctx.workspace(command.workspace_id)
        try:
            preview_target = refresh_preview_target(command.preview_id)
        except ValueError as exc:
            raise _stale_refresh("preview id is invalid") from exc

        def operation() -> WorkspaceRefreshResult:
            with self.ctx.locks.lock(command.workspace_id):
                stored = self.ctx.store.load(command.workspace_id)
                record = replace(stored, metadata=dict(stored.metadata))
                validate_branch(record.branch, repo)
                if record.branch == record.base or record.branch in repo.protected_branches:
                    raise WorkspaceError("Protected or base branches cannot be refreshed")
                old_head, old_fingerprint = require_refresh_snapshot(
                    self.ctx,
                    command.workspace_id,
                    path,
                    command.expected_head_sha,
                    command.expected_fingerprint,
                )
                if self.ctx.git.status_porcelain(path).strip():
                    raise WorkspaceError(
                        "Working tree must be clean before workspace refresh",
                        safe_next_action="Commit or restore current changes, then create a new preview.",
                    )
                base = collect_workspace_base_status(
                    self.ctx,
                    record,
                    repo,
                    path,
                    fetch_remote=True,
                )
                if not base.remote_available or base.remote_base_sha is None:
                    raise WorkspaceError(
                        "REMOTE_BASE_UNAVAILABLE: latest remote base cannot be refreshed",
                        code=ErrorCode.COMMAND_FAILED,
                        retryable=True,
                    )
                if base.remote_base_sha != preview_target:
                    raise _stale_refresh("remote base changed after preview")
                merge_preview = self.ctx.git.preview_merge(path, repo, preview_target)
                binding: WorkspaceRefreshBinding = refresh_binding(
                    workspace_id=command.workspace_id,
                    configured_base=record.base,
                    workspace_base_sha=base.workspace_base_sha,
                    target_base_sha=preview_target,
                    head_sha=old_head,
                    workspace_fingerprint=old_fingerprint,
                    conflict_paths=merge_preview.conflict_paths,
                    workspace_clean=True,
                )
                if binding.preview_id() != command.preview_id:
                    raise _stale_refresh("reviewed merge evidence changed")

                merged = self.ctx.git.merge_no_ff(path, repo, preview_target)
                if merged.status == "conflict":
                    recovered_fingerprint = prime_fingerprint(
                        self.ctx.fingerprint_cache,
                        command.workspace_id,
                        self.ctx.git,
                        path,
                    ).fingerprint
                    recovered = (
                        merged.head_sha == old_head
                        and recovered_fingerprint == old_fingerprint
                        and not self.ctx.git.status_porcelain(path).strip()
                    )
                    if not recovered:
                        raise WorkspaceError(
                            "Workspace refresh conflict did not restore the reviewed state",
                            safe_next_action="Inspect the isolated workspace before further mutation.",
                        )
                    return WorkspaceRefreshResult(
                        command.workspace_id,
                        "conflict",
                        record.base,
                        base.workspace_base_sha,
                        old_head,
                        old_head,
                        list(merged.conflict_paths),
                        [],
                        True,
                        False,
                        "Resolve the reported upstream conflict in a reviewed change, then preview again.",
                    )

                invalidated = invalidate_workspace_refresh_receipts(record)
                record.metadata["workspace_base_sha"] = preview_target
                record.metadata["last_refresh_target_sha"] = preview_target
                record.metadata["last_refresh_at"] = self.ctx.clock.now_iso()
                if merged.head_sha != old_head:
                    record.metadata["refresh_commit_sha"] = merged.head_sha
                try:
                    self.ctx.store.save(record)
                except Exception as exc:
                    if merged.head_sha != old_head:
                        try:
                            self.ctx.git.reset_hard(path, old_head)
                            _ = prime_fingerprint(
                                self.ctx.fingerprint_cache,
                                command.workspace_id,
                                self.ctx.git,
                                path,
                            )
                        except Exception as rollback_exc:
                            raise WorkspaceError(
                                "Workspace refresh merged but registry persistence and rollback both failed",
                                safe_next_action="Inspect the isolated workspace and registry before publishing.",
                            ) from rollback_exc
                    raise WorkspaceError(
                        "Workspace refresh registry update failed; Git state was restored",
                        retryable=True,
                    ) from exc
                _ = prime_fingerprint(
                    self.ctx.fingerprint_cache,
                    command.workspace_id,
                    self.ctx.git,
                    path,
                )
                return WorkspaceRefreshResult(
                    command.workspace_id,
                    merged.status,
                    record.base,
                    preview_target,
                    old_head,
                    merged.head_sha,
                    [],
                    list(invalidated),
                    True,
                    False,
                    (
                        "Run exact-tree verification, then workspace_commit to approve the refresh commit."
                        if merged.head_sha != old_head
                        else "The reviewed base was already integrated; re-run verification before publishing."
                    ),
                )

        return self.ctx.audited(
            "workspace_refresh",
            {
                "workspace_id": command.workspace_id,
                "target_base_sha": preview_target,
            },
            operation,
        )
