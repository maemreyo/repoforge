"""Structured v2 workspace search, tree, and diff evidence."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath

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
    paginate,
    parse_unified_diff,
    search_files,
    tree_entries,
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
    workspace_id: str
    mode: str
    matches: tuple[StructuredSearchMatch, ...]
    head_sha: str
    workspace_fingerprint: str
    truncated: bool
    next_cursor: str | None
    omitted_count: int
    source_truncated: bool


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
            paths, source_truncated = self.ctx.git.list_files(workspace, repo, 10_000)

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
            return WorkspaceSearchV2Result(
                command.workspace_id,
                command.mode.value,
                tuple(page.items),  # type: ignore[arg-type]
                head_sha,
                fingerprint,
                page.truncated or source_truncated,
                page.next_cursor,
                page.omitted_count,
                source_truncated,
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
            head_sha, fingerprint = self._identity(command.workspace_id, workspace)
            source_truncated = False
            if command.staged:
                raw = self.ctx.git.diff(workspace, repo, staged=True)
                files = list(parse_unified_diff(raw["diff"]))
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
                    item = build_diff_file(path, base, current)
                    if item is not None:
                        files.append(item)
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
            return WorkspaceDiffV2Result(
                command.workspace_id,
                command.staged,
                tuple(page.items),  # type: ignore[arg-type]
                self.ctx.git.change_metrics(workspace, repo),
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
