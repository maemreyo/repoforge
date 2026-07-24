"""Structured v2 workspace search, tree, and diff evidence."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import cast

from ...domain.errors import ErrorCode, RepoForgeError, SecurityError
from ...domain.policy import assert_path_allowed, resolve_workspace_path
from ..context import ApplicationContext
from ..fingerprint_cache import read_fingerprint
from ..retrieval import (
    SearchMode,
    StructuredDiffFile,
    StructuredSearchMatch,
    StructuredTreeEntry,
    build_diff_file,
    is_search_scan_cursor,
    paginate,
    parse_unified_diff,
    search_page,
    structured_regex_matches,
    tree_entries,
    validate_path_glob,
    validate_regex,
)


@dataclass(frozen=True, slots=True)
class WorkspaceSearchV2Command:
    workspace_id: str
    query: str
    mode: SearchMode = SearchMode.LITERAL
    path_glob: str | None = None
    max_results: int = 100
    context_lines: int = 0
    byte_budget: int = 60_000
    cursor: str | None = None


@dataclass(frozen=True, slots=True)
class WorkspaceSearchV2Result:
    summary: str
    workspace_id: str
    mode: str
    matches: tuple[StructuredSearchMatch, ...]
    head_sha: str
    workspace_fingerprint: str
    truncated: bool
    next_cursor: str | None
    omitted_count: int
    source_truncated: bool
    truncation_reason: str | None
    scanned_path_count: int
    candidate_path_count: int
    remaining_path_count: int
    completed_providers: tuple[str, ...]
    recommended_scope: str | None


@dataclass(frozen=True, slots=True)
class WorkspaceTreeV2Command:
    workspace_id: str
    subtree: str | None = None
    max_entries: int = 500
    byte_budget: int = 60_000
    cursor: str | None = None


@dataclass(frozen=True, slots=True)
class WorkspaceTreeV2Result:
    workspace_id: str
    subtree: str | None
    entries: tuple[StructuredTreeEntry, ...]
    omitted_count: int
    source_truncated: bool
    head_sha: str
    workspace_fingerprint: str
    truncated: bool
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class WorkspaceDiffV2Command:
    workspace_id: str
    staged: bool = False
    path_glob: str | None = None
    max_files: int = 100
    byte_budget: int = 120_000
    cursor: str | None = None


@dataclass(frozen=True, slots=True)
class WorkspaceDiffV2Result:
    workspace_id: str
    staged: bool
    files: tuple[StructuredDiffFile, ...]
    change_metrics: dict[str, object]
    head_sha: str
    workspace_fingerprint: str
    truncated: bool
    next_cursor: str | None
    omitted_count: int
    source_truncated: bool


class WorkspaceRetrieval:
    def __init__(self, ctx: ApplicationContext):
        self.ctx = ctx

    def _identity(self, workspace_id: str, workspace: Path) -> tuple[str, str]:
        fingerprint = read_fingerprint(
            self.ctx.fingerprint_cache,
            workspace_id,
            self.ctx.git,
            workspace,
        ).fingerprint
        return self.ctx.git.head_sha(workspace), fingerprint

    def search(self, command: WorkspaceSearchV2Command) -> WorkspaceSearchV2Result:
        _, repo, workspace = self.ctx.workspace(command.workspace_id)

        def operation() -> WorkspaceSearchV2Result:
            head_sha, fingerprint = self._identity(command.workspace_id, workspace)
            search_matches: tuple[StructuredSearchMatch, ...]
            completed_providers: tuple[str, ...]
            truncation_reason: str | None
            recommended_scope: str | None
            provider_fallback = False

            def load_text(raw: str) -> str | None:
                normalized = assert_path_allowed(raw, repo)
                unresolved = workspace / normalized
                if unresolved.is_symlink():
                    return None
                path = resolve_workspace_path(workspace, normalized, repo)
                if (
                    not path.is_file()
                    or path.stat().st_size > self.ctx.config.server.max_file_bytes
                ):
                    return None
                data = path.read_bytes()
                if b"\x00" in data:
                    return None
                try:
                    return data.decode("utf-8")
                except UnicodeDecodeError:
                    return None

            if command.mode is SearchMode.REGEX and not is_search_scan_cursor(command.cursor):
                validate_path_glob(command.path_glob)
                validate_regex(command.query)
                try:
                    locations, source_truncated = self.ctx.git.search_regex_locations(
                        workspace,
                        repo,
                        command.query,
                        command.path_glob,
                        10_000,
                        timeout_seconds=1,
                    )
                except RepoForgeError as exc:
                    if exc.code is not ErrorCode.COMMAND_TIMEOUT:
                        raise
                    paths, source_truncated = self.ctx.git.list_files(workspace, repo, 10_000)
                    search = search_page(
                        paths,
                        load_text=load_text,
                        query=command.query,
                        mode=command.mode,
                        path_glob=command.path_glob,
                        context_lines=command.context_lines,
                        kind="workspace_search_v2",
                        scope=f"{command.workspace_id}:{head_sha}:{fingerprint}",
                        max_results=command.max_results,
                        byte_budget=command.byte_budget,
                        cursor=None,
                        source_truncated=source_truncated,
                    )
                    provider_fallback = True
                    search_matches = search.matches
                    next_cursor = search.next_cursor
                    omitted_count = search.omitted_count
                    source_truncated = search.source_truncated
                    truncation_reason = search.truncation_reason
                    scanned_path_count = search.scanned_path_count
                    candidate_path_count = search.candidate_path_count
                    remaining_path_count = search.remaining_path_count
                    completed_providers = search.completed_providers
                    recommended_scope = search.recommended_scope
                else:
                    matches = structured_regex_matches(
                        locations,
                        load_text=load_text,
                        context_lines=command.context_lines,
                    )
                    page = paginate(
                        matches,
                        kind="workspace_search_v2",
                        scope=f"{command.workspace_id}:{head_sha}:{fingerprint}",
                        request={
                            "query": command.query,
                            "mode": command.mode.value,
                            "path_glob": command.path_glob,
                            "context_lines": command.context_lines,
                        },
                        max_items=command.max_results,
                        byte_budget=command.byte_budget,
                        cursor=command.cursor,
                    )
                    truncation_reason = page.truncation_reason or (
                        "source_limit" if source_truncated else None
                    )
                    search_matches = cast(tuple[StructuredSearchMatch, ...], page.items)
                    next_cursor = page.next_cursor
                    omitted_count = page.omitted_count
                    scanned_path_count = len({item.path for item in matches})
                    candidate_path_count = scanned_path_count
                    remaining_path_count = 0
                    completed_providers = ("git_grep_regex",)
                    recommended_scope = (
                        "Narrow path_glob because the regex provider reached its source bound."
                        if source_truncated
                        else "Resume with next_cursor to continue the exact bound search."
                        if next_cursor is not None
                        else None
                    )
            else:
                paths, source_truncated = self.ctx.git.list_files(workspace, repo, 10_000)
                search = search_page(
                    paths,
                    load_text=load_text,
                    query=command.query,
                    mode=command.mode,
                    path_glob=command.path_glob,
                    context_lines=command.context_lines,
                    kind="workspace_search_v2",
                    scope=f"{command.workspace_id}:{head_sha}:{fingerprint}",
                    max_results=command.max_results,
                    byte_budget=command.byte_budget,
                    cursor=command.cursor,
                    source_truncated=source_truncated,
                )
                search_matches = search.matches
                next_cursor = search.next_cursor
                omitted_count = search.omitted_count
                source_truncated = search.source_truncated
                truncation_reason = search.truncation_reason
                scanned_path_count = search.scanned_path_count
                candidate_path_count = search.candidate_path_count
                remaining_path_count = search.remaining_path_count
                completed_providers = search.completed_providers
                recommended_scope = search.recommended_scope
            if truncation_reason == "search_deadline_exceeded":
                self.ctx.record_metric(
                    "workspace_search_v2.partial",
                    success=True,
                    duration_ms=0.0,
                    error_code=None,
                )
            elif truncation_reason == "result_transport_budget":
                self.ctx.record_metric(
                    "workspace_search_v2.transport_budget_preemption",
                    success=True,
                    duration_ms=0.0,
                    error_code=None,
                )
            if provider_fallback:
                self.ctx.record_metric(
                    "workspace_search_v2.provider_fallback",
                    success=True,
                    duration_ms=0.0,
                    error_code=None,
                )
            return WorkspaceSearchV2Result(
                summary=(
                    "Searched the exact workspace tree and returned resumable partial evidence"
                    if next_cursor is not None or source_truncated
                    else "Searched the exact workspace tree"
                ),
                workspace_id=command.workspace_id,
                mode=command.mode.value,
                matches=search_matches,
                head_sha=head_sha,
                workspace_fingerprint=fingerprint,
                truncated=next_cursor is not None or source_truncated,
                next_cursor=next_cursor,
                omitted_count=omitted_count,
                source_truncated=source_truncated,
                truncation_reason=truncation_reason,
                scanned_path_count=scanned_path_count,
                candidate_path_count=candidate_path_count,
                remaining_path_count=remaining_path_count,
                completed_providers=completed_providers,
                recommended_scope=recommended_scope,
            )

        return self.ctx.audited(
            "workspace_search_v2",
            {
                "workspace_id": command.workspace_id,
                "mode": command.mode.value,
                "max_results": command.max_results,
            },
            operation,
        )

    def tree(self, command: WorkspaceTreeV2Command) -> WorkspaceTreeV2Result:
        _, repo, workspace = self.ctx.workspace(command.workspace_id)

        def operation() -> WorkspaceTreeV2Result:
            head_sha, fingerprint = self._identity(command.workspace_id, workspace)
            paths, source_truncated = self.ctx.git.list_files(workspace, repo, 10_000)

            def size_of(raw: str) -> int | None:
                normalized = assert_path_allowed(raw, repo)
                path = resolve_workspace_path(workspace, normalized, repo)
                if path.is_symlink() or not path.is_file():
                    return None
                return path.stat().st_size

            entries = tree_entries(paths, subtree=command.subtree, size_of=size_of)
            page = paginate(
                entries,
                kind="workspace_tree_v2",
                scope=f"{command.workspace_id}:{head_sha}:{fingerprint}",
                request={"subtree": command.subtree},
                max_items=command.max_entries,
                byte_budget=command.byte_budget,
                cursor=command.cursor,
            )
            return WorkspaceTreeV2Result(
                command.workspace_id,
                command.subtree,
                tuple(page.items),  # type: ignore[arg-type]
                page.omitted_count,
                source_truncated,
                head_sha,
                fingerprint,
                page.truncated or source_truncated,
                page.next_cursor,
            )

        return self.ctx.audited(
            "workspace_tree_v2",
            {
                "workspace_id": command.workspace_id,
                "subtree": command.subtree,
                "max_entries": command.max_entries,
            },
            operation,
        )

    def diff(self, command: WorkspaceDiffV2Command) -> WorkspaceDiffV2Result:
        _, repo, workspace = self.ctx.workspace(command.workspace_id)

        def operation() -> WorkspaceDiffV2Result:
            validate_path_glob(command.path_glob)
            head_sha, fingerprint = self._identity(command.workspace_id, workspace)
            source_truncated = False
            if command.staged:
                raw = self.ctx.git.diff(workspace, repo, staged=True)
                files = list(parse_unified_diff(raw["diff"]))
                for parsed_file in files:
                    assert_path_allowed(parsed_file.path, repo)
                source_truncated = bool(raw["truncated"])
            else:
                files = []
                for raw_path in sorted(self.ctx.git.changed_paths(workspace, repo)):
                    path = assert_path_allowed(raw_path, repo)
                    if command.path_glob is not None and not PurePosixPath(path).match(
                        command.path_glob
                    ):
                        continue
                    try:
                        base = self.ctx.git.read_snapshot_blob(workspace, repo, head_sha, path).data
                    except RepoForgeError as exc:
                        if exc.code is not ErrorCode.NOT_FOUND:
                            raise
                        base = None
                    candidate = resolve_workspace_path(workspace, path, repo)
                    if candidate.is_file() and not candidate.is_symlink():
                        if candidate.stat().st_size > self.ctx.config.server.max_file_bytes:
                            raise SecurityError(f"Diff target exceeds max_file_bytes: {path}")
                        current = candidate.read_bytes()
                    else:
                        current = None
                    diff_file = build_diff_file(path, base, current)
                    if diff_file is not None:
                        files.append(diff_file)
            if command.path_glob is not None and command.staged:
                files = [
                    item for item in files if PurePosixPath(item.path).match(command.path_glob)
                ]
            page = paginate(
                files,
                kind="workspace_diff_v2",
                scope=f"{command.workspace_id}:{head_sha}:{fingerprint}",
                request={
                    "staged": command.staged,
                    "path_glob": command.path_glob,
                },
                max_items=command.max_files,
                byte_budget=command.byte_budget,
                cursor=command.cursor,
            )
            raw_metrics = self.ctx.git.change_metrics(workspace, repo)
            public_metrics = {
                key: raw_metrics[key]
                for key in (
                    "changed_files",
                    "added_lines",
                    "deleted_lines",
                    "diff_lines",
                    "total_current_bytes",
                    "within_limits",
                )
            }
            return WorkspaceDiffV2Result(
                command.workspace_id,
                command.staged,
                tuple(page.items),  # type: ignore[arg-type]
                public_metrics,
                head_sha,
                fingerprint,
                page.truncated or source_truncated,
                page.next_cursor,
                page.omitted_count,
                source_truncated,
            )

        return self.ctx.audited(
            "workspace_diff_v2",
            {
                "workspace_id": command.workspace_id,
                "staged": command.staged,
                "path_glob": command.path_glob,
                "max_files": command.max_files,
            },
            operation,
        )
