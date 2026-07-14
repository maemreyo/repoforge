from __future__ import annotations

import contextlib
from dataclasses import dataclass
from pathlib import Path

from ...config import RepositoryConfig
from ...domain.workspace import WorkspaceRecord, is_commit_sha
from ..context import ApplicationContext


@dataclass(frozen=True, slots=True)
class WorkspaceBaseStatusCommand:
    workspace_id: str


@dataclass(frozen=True, slots=True)
class WorkspaceBaseStatusResult:
    workspace_id: str
    repo_id: str
    configured_base: str
    workspace_base_sha: str
    workspace_base_source: str
    local_base_sha: str
    remote_base_sha: str | None
    latest_base_sha: str
    head_sha: str
    ahead_base: int
    behind_base: int
    local_remote_relation: str
    upstream_changed_paths: list[str]
    workspace_changed_paths: list[str]
    overlap_paths: list[str]
    remote_available: bool
    remote_error_code: str | None
    staleness: str
    refresh_required: bool
    published_state: str
    last_pushed_sha: str | None
    upstream_name: str | None
    upstream_sha: str | None


def collect_workspace_base_status(
    ctx: ApplicationContext,
    record: WorkspaceRecord,
    repo: RepositoryConfig,
    path: Path,
    *,
    fetch_remote: bool,
) -> WorkspaceBaseStatusResult:
    refs = ctx.git.inspect_base_references(
        path,
        record.remote,
        record.base,
        fetch_remote=fetch_remote,
    )
    head = ctx.git.head_sha(path)
    latest = refs.remote_sha if refs.remote_available and refs.remote_sha else refs.local_sha
    raw_workspace_base = record.metadata.get("workspace_base_sha")
    if is_commit_sha(raw_workspace_base):
        workspace_base = str(raw_workspace_base)
        workspace_base_source = "recorded"
    else:
        workspace_base = ctx.git.merge_base(path, head, latest)
        workspace_base_source = "inferred"
    ahead, behind = ctx.git.ahead_behind(path, head, latest)
    upstream_paths = ctx.git.changed_paths_between(path, repo, workspace_base, latest)
    workspace_paths = set(ctx.git.changed_paths_between(path, repo, workspace_base, head))
    workspace_paths.update(ctx.git.changed_paths(path, repo))
    ordered_workspace_paths = sorted(workspace_paths)
    overlap = sorted(set(upstream_paths).intersection(workspace_paths))

    if not refs.remote_available:
        staleness = "unavailable_remote"
    elif refs.local_sha == refs.remote_sha == workspace_base:
        staleness = "current"
    elif refs.local_sha == refs.remote_sha:
        staleness = "local_base_stale"
    elif refs.relation == "local_behind_remote" and workspace_base == refs.local_sha:
        staleness = "remote_base_stale"
    else:
        staleness = "diverged"

    upstream_name = ctx.git.upstream_name(path)
    upstream_sha: str | None = None
    if upstream_name:
        with contextlib.suppress(Exception):
            upstream_sha = ctx.git.upstream_sha(path)
    last_pushed_raw = record.metadata.get("last_pushed_sha")
    last_pushed = str(last_pushed_raw) if is_commit_sha(last_pushed_raw) else None
    if last_pushed is None:
        published_state = "unpublished"
    elif last_pushed == head and upstream_sha == head:
        published_state = "published_current"
    else:
        published_state = "published_stale"

    return WorkspaceBaseStatusResult(
        record.workspace_id,
        record.repo_id,
        record.base,
        workspace_base,
        workspace_base_source,
        refs.local_sha,
        refs.remote_sha,
        latest,
        head,
        ahead,
        behind,
        refs.relation,
        upstream_paths,
        ordered_workspace_paths,
        overlap,
        refs.remote_available,
        refs.remote_error_code,
        staleness,
        staleness != "current",
        published_state,
        last_pushed,
        upstream_name,
        upstream_sha,
    )


class WorkspaceBaseStatusReader:
    def __init__(self, ctx: ApplicationContext):
        self.ctx = ctx

    def execute(self, command: WorkspaceBaseStatusCommand) -> WorkspaceBaseStatusResult:
        _, repo, path = self.ctx.workspace(command.workspace_id)

        def operation() -> WorkspaceBaseStatusResult:
            with self.ctx.locks.lock(command.workspace_id):
                fresh = self.ctx.store.load(command.workspace_id)
                return collect_workspace_base_status(
                    self.ctx,
                    fresh,
                    repo,
                    path,
                    fetch_remote=True,
                )

        return self.ctx.audited(
            "workspace_base_status",
            {"workspace_id": command.workspace_id},
            operation,
            mutating=False,
        )
