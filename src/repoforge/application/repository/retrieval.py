"""Structured v2 repository search and tree evidence."""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

from ...domain.errors import ErrorCode, RepoForgeError, SecurityError
from ..context import ApplicationContext
from ..retrieval import (
    SearchMode,
    StructuredSearchMatch,
    StructuredTreeEntry,
    is_search_scan_cursor,
    paginate,
    search_page,
    structured_regex_matches,
    tree_entries,
    validate_path_glob,
    validate_regex,
)


@dataclass(frozen=True, slots=True)
class RepositorySearchV2Command:
    repo_id: str
    query: str
    mode: SearchMode = SearchMode.LITERAL
    ref: str | None = None
    path_glob: str | None = None
    max_results: int = 100
    context_lines: int = 0
    byte_budget: int = 60_000
    cursor: str | None = None


@dataclass(frozen=True, slots=True)
class RepositorySearchV2Result:
    summary: str
    repo_id: str
    resolved_ref: str
    commit_sha: str
    mode: str
    matches: tuple[StructuredSearchMatch, ...]
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
class RepositoryTreeV2Command:
    repo_id: str
    ref: str | None = None
    subtree: str | None = None
    max_entries: int = 500
    byte_budget: int = 60_000
    cursor: str | None = None


@dataclass(frozen=True, slots=True)
class RepositoryTreeV2Result:
    repo_id: str
    resolved_ref: str
    commit_sha: str
    subtree: str | None
    entries: tuple[StructuredTreeEntry, ...]
    omitted_count: int
    source_truncated: bool
    truncated: bool
    next_cursor: str | None


class RepositoryRetrieval:
    def __init__(self, ctx: ApplicationContext):
        self.ctx = ctx

    def search(self, command: RepositorySearchV2Command) -> RepositorySearchV2Result:
        repo = self.ctx.repo(command.repo_id)

        def operation() -> RepositorySearchV2Result:
            snapshot = self.ctx.git.resolve_snapshot_ref(repo.path, repo, command.ref)
            cache: dict[str, str | None] = {}
            search_matches: tuple[StructuredSearchMatch, ...]
            completed_providers: tuple[str, ...]
            truncation_reason: str | None
            recommended_scope: str | None
            provider_fallback = False

            def load_text(path: str) -> str | None:
                if path in cache:
                    return cache[path]
                blob = self.ctx.git.read_snapshot_blob(repo.path, repo, snapshot.commit_sha, path)
                if blob.size_bytes > self.ctx.config.server.max_file_bytes:
                    cache[path] = None
                    return None
                if b"\x00" in blob.data:
                    cache[path] = None
                    return None
                try:
                    text = blob.data.decode("utf-8")
                except UnicodeDecodeError:
                    text = None
                cache[path] = text
                return text

            if command.mode is SearchMode.REGEX and not is_search_scan_cursor(command.cursor):
                validate_path_glob(command.path_glob)
                validate_regex(command.query)
                try:
                    locations, source_truncated = self.ctx.git.search_regex_locations(
                        repo.path,
                        repo,
                        command.query,
                        command.path_glob,
                        10_000,
                        commit_sha=snapshot.commit_sha,
                        timeout_seconds=1,
                    )
                except RepoForgeError as exc:
                    if exc.code is not ErrorCode.COMMAND_TIMEOUT:
                        raise
                    paths, source_truncated = self.ctx.git.list_snapshot_files(
                        repo.path, repo, snapshot.commit_sha, 10_000
                    )
                    search = search_page(
                        paths,
                        load_text=load_text,
                        query=command.query,
                        mode=command.mode,
                        path_glob=command.path_glob,
                        context_lines=command.context_lines,
                        kind="repo_search_v2",
                        scope=f"{command.repo_id}:{snapshot.commit_sha}",
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
                        kind="repo_search_v2",
                        scope=f"{command.repo_id}:{snapshot.commit_sha}",
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
                paths, source_truncated = self.ctx.git.list_snapshot_files(
                    repo.path, repo, snapshot.commit_sha, 10_000
                )
                search = search_page(
                    paths,
                    load_text=load_text,
                    query=command.query,
                    mode=command.mode,
                    path_glob=command.path_glob,
                    context_lines=command.context_lines,
                    kind="repo_search_v2",
                    scope=f"{command.repo_id}:{snapshot.commit_sha}",
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
                    "repo_search_v2.partial",
                    success=True,
                    duration_ms=0.0,
                    error_code=None,
                )
            elif truncation_reason == "result_transport_budget":
                self.ctx.record_metric(
                    "repo_search_v2.transport_budget_preemption",
                    success=True,
                    duration_ms=0.0,
                    error_code=None,
                )
            if provider_fallback:
                self.ctx.record_metric(
                    "repo_search_v2.provider_fallback",
                    success=True,
                    duration_ms=0.0,
                    error_code=None,
                )
            return RepositorySearchV2Result(
                summary=(
                    "Searched the exact repository snapshot and returned resumable partial evidence"
                    if next_cursor is not None or source_truncated
                    else "Searched the exact repository snapshot"
                ),
                repo_id=command.repo_id,
                resolved_ref=snapshot.resolved_ref,
                commit_sha=snapshot.commit_sha,
                mode=command.mode.value,
                matches=search_matches,
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
            "repo_search_v2",
            {
                "repo_id": command.repo_id,
                "mode": command.mode.value,
                "max_results": command.max_results,
            },
            operation,
        )

    def tree(self, command: RepositoryTreeV2Command) -> RepositoryTreeV2Result:
        repo = self.ctx.repo(command.repo_id)

        def operation() -> RepositoryTreeV2Result:
            snapshot = self.ctx.git.resolve_snapshot_ref(repo.path, repo, command.ref)
            paths, source_truncated = self.ctx.git.list_snapshot_files(
                repo.path, repo, snapshot.commit_sha, 10_000
            )
            sizes: dict[str, int | None] = {}

            def size_of(path: str) -> int | None:
                if path not in sizes:
                    try:
                        sizes[path] = self.ctx.git.read_snapshot_blob(
                            repo.path, repo, snapshot.commit_sha, path
                        ).size_bytes
                    except (SecurityError, ValueError):
                        sizes[path] = None
                return sizes[path]

            entries = tree_entries(paths, subtree=command.subtree, size_of=size_of)
            page = paginate(
                entries,
                kind="repo_tree_v2",
                scope=f"{command.repo_id}:{snapshot.commit_sha}",
                request={"subtree": command.subtree},
                max_items=command.max_entries,
                byte_budget=command.byte_budget,
                cursor=command.cursor,
            )
            return RepositoryTreeV2Result(
                command.repo_id,
                snapshot.resolved_ref,
                snapshot.commit_sha,
                command.subtree,
                tuple(page.items),  # type: ignore[arg-type]
                page.omitted_count,
                source_truncated,
                page.truncated or source_truncated,
                page.next_cursor,
            )

        return self.ctx.audited(
            "repo_tree_v2",
            {
                "repo_id": command.repo_id,
                "subtree": command.subtree,
                "max_entries": command.max_entries,
            },
            operation,
        )
