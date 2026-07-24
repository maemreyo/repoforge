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
    generated_overlap_paths: list[str]
    expected_evidence_invalidation: list[str]
    verify_selector: list[str]
    recommended_action: str
    preflight_warning: str | None
    recreate_eligible: bool
    recreate_blockers: list[str]


_EXPECTED_REFRESH_INVALIDATION = [
    "last_verification",
    "code_intelligence",
    "diagnostic_receipts",
    "failure_evidence",
]


def freshness_preflight_payload(result: WorkspaceBaseStatusResult) -> dict[str, object]:
    """Project one stable pre-mutation/pre-verification freshness contract."""

    return {
        "staleness": result.staleness,
        "refresh_required": result.refresh_required,
        "workspace_base_sha": result.workspace_base_sha,
        "latest_base_sha": result.latest_base_sha,
        "head_sha": result.head_sha,
        "ahead_base": result.ahead_base,
        "behind_base": result.behind_base,
        "upstream_changed_paths": list(result.upstream_changed_paths),
        "workspace_changed_paths": list(result.workspace_changed_paths),
        "overlap_paths": list(result.overlap_paths),
        "generated_overlap_paths": list(result.generated_overlap_paths),
        "expected_evidence_invalidation": list(result.expected_evidence_invalidation),
        "verify_selector": list(result.verify_selector),
        "recommended_action": result.recommended_action,
        "warning": result.preflight_warning,
        "recreate_eligible": result.recreate_eligible,
        "recreate_blockers": list(result.recreate_blockers),
        "remote_available": result.remote_available,
        "remote_error_code": result.remote_error_code,
    }


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
    generated_overlap = sorted(
        path for path in overlap if any(rule.matches(path) for rule in repo.generated_paths)
    )

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

    clean = not bool(ctx.git.status_porcelain(path).strip())
    recreate_blockers: list[str] = []
    if not clean:
        recreate_blockers.append("working_tree_not_clean")
    if ahead != 0:
        recreate_blockers.append("unique_commits_present")
    if published_state != "unpublished":
        recreate_blockers.append("published_branch")
    if record.metadata.get("pr_url") or record.metadata.get("pr_number"):
        recreate_blockers.append("pull_request_bound")
    if not isinstance(record.metadata.get("task_slug"), str) or not isinstance(
        record.metadata.get("workspace_create_idempotency"), str
    ):
        recreate_blockers.append("task_binding_unavailable")
    if record.metadata.get("external_write_count") not in {None, 0}:
        recreate_blockers.append("external_writes_recorded")
    recreate_eligible = staleness != "current" and refs.remote_available and not recreate_blockers

    if staleness == "current":
        recommended_action = "continue"
        warning = None
    elif not refs.remote_available:
        recommended_action = "restore_remote_connectivity"
        warning = (
            "Base freshness cannot be established before mutation or verification because the "
            "remote base is unavailable."
        )
    elif recreate_eligible:
        recommended_action = "recreate_from_latest_base"
        warning = (
            "Base is stale before mutation or full verification. The clean task-bound workspace "
            "can be recreated from the latest base; refresh or recreate will invalidate "
            + ", ".join(_EXPECTED_REFRESH_INVALIDATION)
            + "."
        )
    else:
        recommended_action = "refresh_preview"
        overlap_note = (
            " generated-path overlap: " + ", ".join(generated_overlap) + "."
            if generated_overlap
            else " Overlap forecast: " + ", ".join(overlap) + "."
            if overlap
            else ""
        )
        warning = (
            "Base is stale before mutation or full verification; review workspace_refresh preview "
            "first because later refresh will invalidate "
            + ", ".join(_EXPECTED_REFRESH_INVALIDATION)
            + "."
            + overlap_note
        )
    verify_selector = sorted(set(upstream_paths).union(overlap, generated_overlap))

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
        generated_overlap,
        list(_EXPECTED_REFRESH_INVALIDATION),
        verify_selector,
        recommended_action,
        warning,
        recreate_eligible,
        recreate_blockers,
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
