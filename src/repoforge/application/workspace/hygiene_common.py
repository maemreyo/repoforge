"""Shared policy selection and bounded path derivation for hygiene use cases."""

from __future__ import annotations

import fnmatch
import hashlib
from pathlib import Path, PurePosixPath

from ...config import RepositoryConfig
from ...domain.errors import ConfigError, SecurityError, WorkspaceError
from ...domain.hygiene import FormatterPolicy, HygieneFinding
from ...domain.policy import assert_path_allowed
from ..context import ApplicationContext

_MAX_REPOSITORY_ENTRIES = 20_000


def select_formatter(
    repo: RepositoryConfig,
    formatter_id: str | None,
    *,
    allow_unavailable: bool,
) -> FormatterPolicy | None:
    if formatter_id is not None:
        try:
            return repo.formatters[formatter_id]
        except KeyError as exc:
            raise ConfigError(f"Unknown reviewed formatter: {formatter_id}") from exc
    if len(repo.formatters) == 1:
        return next(iter(repo.formatters.values()))
    if allow_unavailable:
        return None
    if not repo.formatters:
        raise ConfigError(
            "No reviewed formatter is configured for this repository",
            safe_next_action=(
                "Propose a formatter policy with fixed check/fix argv, include globs, and bounds."
            ),
        )
    raise ConfigError(
        "formatter_id is required when several reviewed formatters are configured",
        safe_next_action="Choose one formatter_id returned by repo_list.",
    )


def _matches(path: str, patterns: tuple[str, ...]) -> bool:
    candidate = PurePosixPath(path)
    return any(
        fnmatch.fnmatchcase(path, pattern) or candidate.match(pattern) for pattern in patterns
    )


def select_policy_paths(
    entries: list[str],
    *,
    repo: RepositoryConfig,
    policy: FormatterPolicy,
) -> tuple[str, ...]:
    selected: list[str] = []
    for raw in sorted(set(entries)):
        try:
            path = assert_path_allowed(raw, repo)
        except SecurityError:
            continue
        if _matches(path, policy.include_globs):
            selected.append(path)
    if len(selected) > policy.max_paths:
        raise WorkspaceError(
            f"Formatter selection found {len(selected)} paths, exceeding max_paths={policy.max_paths}"
        )
    return tuple(selected)


def workspace_policy_paths(
    ctx: ApplicationContext,
    workspace: Path,
    repo: RepositoryConfig,
    policy: FormatterPolicy,
) -> tuple[str, ...]:
    entries, truncated = ctx.git.list_files(workspace, repo, _MAX_REPOSITORY_ENTRIES)
    if truncated:
        raise WorkspaceError(
            f"Repository file listing exceeds {_MAX_REPOSITORY_ENTRIES}; hygiene scope is unavailable"
        )
    return select_policy_paths(entries, repo=repo, policy=policy)


def base_policy_paths(
    ctx: ApplicationContext,
    workspace: Path,
    repo: RepositoryConfig,
    base_sha: str,
    policy: FormatterPolicy,
) -> tuple[str, ...]:
    entries, truncated = ctx.git.list_snapshot_files(
        workspace,
        repo,
        base_sha,
        _MAX_REPOSITORY_ENTRIES,
    )
    if truncated:
        raise WorkspaceError(
            f"Base snapshot file listing exceeds {_MAX_REPOSITORY_ENTRIES}; hygiene scope is unavailable"
        )
    return select_policy_paths(entries, repo=repo, policy=policy)


def workspace_base_sha(
    ctx: ApplicationContext,
    workspace: Path,
    repo: RepositoryConfig,
    configured_base: str,
) -> str:
    head_sha = ctx.git.head_sha(workspace)
    current_base = ctx.git.resolve_snapshot_ref(
        workspace,
        repo,
        configured_base,
    )
    return ctx.git.merge_base(workspace, head_sha, current_base.commit_sha)


def config_identity(ctx: ApplicationContext) -> str:
    try:
        data = ctx.config.source_path.read_bytes()
    except OSError as exc:
        raise ConfigError(
            "Active configuration file is unavailable for hygiene cache identity"
        ) from exc
    return hashlib.sha256(data).hexdigest()


def finding_data(finding: HygieneFinding) -> dict[str, str]:
    return {
        "identity": finding.identity,
        "message": finding.message,
        "path": finding.path,
        "rule": finding.rule,
    }
