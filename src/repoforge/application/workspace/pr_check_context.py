"""Shared exact-SHA loading for workspace Check Run evidence."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ...config import RepositoryConfig
from ...domain.ci_evidence import parse_check_selector
from ...domain.errors import CommandError, ErrorCode, RepoForgeError
from ...ports.github import GitHubActionsJob, GitHubCheckAnnotation, GitHubCheckRun
from ..context import ApplicationContext


@dataclass(frozen=True, slots=True)
class WorkspaceCheckContext:
    workspace_id: str
    branch: str
    repo: RepositoryConfig
    path: Path
    selector: str
    pushed_sha: str
    check: GitHubCheckRun
    annotations: tuple[GitHubCheckAnnotation, ...]
    annotations_truncated: bool
    job: GitHubActionsJob | None
    source_errors: tuple[str, ...]


def _source_error(prefix: str, exc: CommandError) -> str:
    message = str(exc).lower()
    if "not accessible" in message or "permission" in message or "forbidden" in message:
        return f"{prefix}_permission_denied"
    if "not found" in message or "missing" in message:
        return f"{prefix}_not_found"
    return f"{prefix}_unavailable"


def load_workspace_check_context(
    ctx: ApplicationContext,
    workspace_id: str,
    selector: str,
) -> WorkspaceCheckContext:
    record, repo, path = ctx.workspace(workspace_id)
    check_run_id = parse_check_selector(selector)
    pushed_raw = record.metadata.get("last_pushed_sha")
    if not isinstance(pushed_raw, str) or not pushed_raw:
        raise RepoForgeError(
            "CI evidence requires a successfully pushed workspace commit",
            code=ErrorCode.CHECK_EVIDENCE_STALE,
            safe_next_action="Verify, commit, and push the workspace before reading CI evidence.",
        )
    pushed_sha = pushed_raw.lower()
    current_sha = ctx.git.head_sha(path).lower()
    if current_sha != pushed_sha:
        raise RepoForgeError(
            "Workspace HEAD changed after the commit associated with this CI evidence was pushed",
            code=ErrorCode.CHECK_EVIDENCE_STALE,
            retryable=True,
            safe_next_action="Verify, commit, and push the current workspace HEAD, then refresh PR checks.",
        )
    try:
        check = ctx.github.check_run(path, check_run_id)
    except CommandError as exc:
        raise RepoForgeError(
            "Primary GitHub Check Run evidence is unavailable",
            code=ErrorCode.CHECK_EVIDENCE_UNAVAILABLE,
            retryable=True,
            safe_next_action="Confirm GitHub access and refresh workspace_pr_checks before retrying.",
        ) from exc
    if check.head_sha.lower() != pushed_sha:
        raise RepoForgeError(
            "Selected Check Run belongs to a different commit than the pushed workspace HEAD",
            code=ErrorCode.CHECK_EVIDENCE_STALE,
            retryable=True,
            safe_next_action="Call workspace_pr_checks and select a check bound to the current pushed SHA.",
        )

    source_errors: list[str] = []
    annotations: list[GitHubCheckAnnotation] = []
    annotations_truncated = False
    try:
        annotations, annotations_truncated = ctx.github.check_annotations(
            path,
            check_run_id,
            max_annotations=50,
        )
    except CommandError as exc:
        source_errors.append(_source_error("annotations", exc))

    job: GitHubActionsJob | None = None
    if check.job_id is not None:
        try:
            job = ctx.github.actions_job(path, check.job_id)
        except CommandError as exc:
            source_errors.append(_source_error("job_metadata", exc))

    return WorkspaceCheckContext(
        workspace_id=workspace_id,
        branch=record.branch,
        repo=repo,
        path=path,
        selector=selector,
        pushed_sha=pushed_sha,
        check=check,
        annotations=tuple(annotations),
        annotations_truncated=annotations_truncated,
        job=job,
        source_errors=tuple(source_errors),
    )


def source_error_label(prefix: str, exc: CommandError) -> str:
    """Expose stable optional-source labels without propagating raw external errors."""
    return _source_error(prefix, exc)
