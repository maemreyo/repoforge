"""Unified repository snapshot batch reader."""

from __future__ import annotations

from dataclasses import dataclass

from ...domain.errors import SecurityError
from ..context import ApplicationContext
from ..read_batch import (
    FileReadRequest,
    LoadedTextFile,
    ReadFileError,
    ReadFileResult,
    execute_batch_read,
)


@dataclass(frozen=True, slots=True)
class RepositoryReadCommand:
    repo_id: str
    files: tuple[FileReadRequest, ...]
    ref: str | None = None
    byte_budget: int = 60_000
    cursor: str | None = None


@dataclass(frozen=True, slots=True)
class RepositoryReadResult:
    repo_id: str
    resolved_ref: str
    commit_sha: str
    files: tuple[ReadFileResult, ...]
    errors: tuple[ReadFileError, ...]
    requested: int
    succeeded: int
    truncated: bool
    next_cursor: str | None


class RepositoryReader:
    def __init__(self, ctx: ApplicationContext):
        self.ctx = ctx

    def execute(self, command: RepositoryReadCommand) -> RepositoryReadResult:
        repo = self.ctx.repo(command.repo_id)

        def operation() -> RepositoryReadResult:
            snapshot = self.ctx.git.resolve_snapshot_ref(repo.path, repo, command.ref)

            def load(path: str) -> LoadedTextFile:
                blob = self.ctx.git.read_snapshot_blob(
                    repo.path,
                    repo,
                    snapshot.commit_sha,
                    path,
                )
                if blob.size_bytes > self.ctx.config.server.max_file_bytes:
                    raise SecurityError(f"File exceeds max_file_bytes: {blob.path}")
                return LoadedTextFile(blob.path, blob.data)

            batch = execute_batch_read(
                kind="repo_read",
                scope=f"{command.repo_id}:{snapshot.commit_sha}",
                requests=command.files,
                loader=load,
                byte_budget=command.byte_budget,
                cursor=command.cursor,
            )
            return RepositoryReadResult(
                repo_id=command.repo_id,
                resolved_ref=snapshot.resolved_ref,
                commit_sha=snapshot.commit_sha,
                files=batch.files,
                errors=batch.errors,
                requested=batch.requested,
                succeeded=batch.succeeded,
                truncated=batch.truncated,
                next_cursor=batch.next_cursor,
            )

        return self.ctx.audited(
            "repo_read",
            {
                "repo_id": command.repo_id,
                "ref": command.ref,
                "file_count": len(command.files),
                "byte_budget": command.byte_budget,
                "resumed": command.cursor is not None,
            },
            operation,
        )
