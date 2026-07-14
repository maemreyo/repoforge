from __future__ import annotations

from dataclasses import dataclass

from ..context import ApplicationContext


@dataclass(frozen=True, slots=True)
class RepositoryTreeCommand:
    repo_id: str
    ref: str | None = None
    max_entries: int = 2000


@dataclass(frozen=True, slots=True)
class RepositoryTreeResult:
    repo_id: str
    resolved_ref: str
    commit_sha: str
    entries: list[str]
    truncated: bool


class RepositoryTreeReader:
    def __init__(self, ctx: ApplicationContext):
        self.ctx = ctx

    def execute(self, command: RepositoryTreeCommand) -> RepositoryTreeResult:
        repo = self.ctx.repo(command.repo_id)
        limit = max(1, min(command.max_entries, 10_000))

        def op() -> RepositoryTreeResult:
            snapshot = self.ctx.git.resolve_snapshot_ref(repo.path, repo, command.ref)
            entries, truncated = self.ctx.git.list_snapshot_files(
                repo.path,
                repo,
                snapshot.commit_sha,
                limit,
            )
            return RepositoryTreeResult(
                command.repo_id,
                snapshot.resolved_ref,
                snapshot.commit_sha,
                entries,
                truncated,
            )

        return self.ctx.audited(
            "repo_tree",
            {"repo_id": command.repo_id, "ref": command.ref, "max_entries": limit},
            op,
        )
