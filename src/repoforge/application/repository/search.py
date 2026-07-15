from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath

from ...domain.errors import SecurityError
from ..context import ApplicationContext


@dataclass(frozen=True, slots=True)
class RepositorySearchCommand:
    repo_id: str
    query: str
    ref: str | None = None
    path_glob: str | None = None
    max_results: int = 200
    context_lines: int = 0


@dataclass(frozen=True, slots=True)
class RepositorySearchResult:
    repo_id: str
    resolved_ref: str
    commit_sha: str
    query: str
    matches: list[str]
    truncated: bool


class RepositorySearcher:
    def __init__(self, ctx: ApplicationContext):
        self.ctx = ctx

    def execute(self, command: RepositorySearchCommand) -> RepositorySearchResult:
        if not command.query or "\x00" in command.query:
            raise ValueError("query must be non-empty")
        if command.path_glob:
            glob_path = PurePosixPath(command.path_glob)
            if (
                command.path_glob.startswith(("/", "-", ":"))
                or any(ord(character) < 32 for character in command.path_glob)
                or ".." in glob_path.parts
            ):
                raise SecurityError("Unsafe path_glob")
        if not (0 <= command.context_lines <= 5):
            raise ValueError("context_lines must be between 0 and 5")
        limit = max(1, min(command.max_results, 2000))
        repo = self.ctx.repo(command.repo_id)

        def op() -> RepositorySearchResult:
            snapshot = self.ctx.git.resolve_snapshot_ref(repo.path, repo, command.ref)
            matches, truncated = self.ctx.git.search_snapshot(
                repo.path,
                repo,
                snapshot.commit_sha,
                command.query,
                command.path_glob,
                limit,
                command.context_lines,
            )
            return RepositorySearchResult(
                command.repo_id,
                snapshot.resolved_ref,
                snapshot.commit_sha,
                command.query,
                matches,
                truncated,
            )

        return self.ctx.audited(
            "repo_search",
            {
                "repo_id": command.repo_id,
                "ref": command.ref,
                "path_glob": command.path_glob,
                "max_results": limit,
                "context_lines": command.context_lines,
            },
            op,
        )
