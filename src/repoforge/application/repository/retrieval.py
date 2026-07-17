"""Structured v2 repository search and tree evidence."""

from __future__ import annotations

from dataclasses import dataclass

from ...domain.errors import SecurityError
from ..context import ApplicationContext
from ..retrieval import (
    SearchMode,
    StructuredSearchMatch,
    StructuredTreeEntry,
    paginate,
    search_files,
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
    repo_id: str
    resolved_ref: str
    commit_sha: str
    mode: str
    matches: tuple[StructuredSearchMatch, ...]
    truncated: bool
    next_cursor: str | None
    omitted_count: int
    source_truncated: bool


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

            if command.mode is SearchMode.REGEX:
                validate_path_glob(command.path_glob)
                validate_regex(command.query)
                locations, source_truncated = self.ctx.git.search_regex_locations(
                    repo.path,
                    repo,
                    command.query,
                    command.path_glob,
                    10_000,
                    commit_sha=snapshot.commit_sha,
                    timeout_seconds=1,
                )
                matches = structured_regex_matches(
                    locations,
                    load_text=load_text,
                    context_lines=command.context_lines,
                )
            else:
                paths, source_truncated = self.ctx.git.list_snapshot_files(
                    repo.path, repo, snapshot.commit_sha, 10_000
                )
                matches = search_files(
                    paths,
                    load_text=load_text,
                    query=command.query,
                    mode=command.mode,
                    path_glob=command.path_glob,
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
            return RepositorySearchV2Result(
                command.repo_id,
                snapshot.resolved_ref,
                snapshot.commit_sha,
                command.mode.value,
                tuple(page.items),  # type: ignore[arg-type]
                page.truncated or source_truncated,
                page.next_cursor,
                page.omitted_count,
                source_truncated,
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
