from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ...domain.errors import ErrorCode, RepoForgeError
from ..context import ApplicationContext
from .file_read import RepositoryFileReader


@dataclass(frozen=True, slots=True)
class RepositoryFilesReadCommand:
    repo_id: str
    relative_paths: list[str]
    ref: str | None = None
    start_line: int = 1
    end_line: int = 500


@dataclass(frozen=True, slots=True)
class RepositoryFilesReadResult:
    repo_id: str
    resolved_ref: str
    commit_sha: str
    files: list[dict[str, Any]]
    errors: list[dict[str, str]]
    requested: int
    succeeded: int


class RepositoryFilesReader:
    def __init__(self, ctx: ApplicationContext):
        self.ctx = ctx
        self.reader = RepositoryFileReader(ctx)

    def execute(self, command: RepositoryFilesReadCommand) -> RepositoryFilesReadResult:
        if not command.relative_paths:
            raise ValueError("relative_paths must contain at least one path")
        if len(command.relative_paths) > self.ctx.config.server.max_batch_files:
            raise ValueError(
                f"relative_paths exceeds max_batch_files={self.ctx.config.server.max_batch_files}"
            )
        unique = list(dict.fromkeys(command.relative_paths))
        repo = self.ctx.repo(command.repo_id)

        def op() -> RepositoryFilesReadResult:
            snapshot = self.ctx.git.resolve_snapshot_ref(repo.path, repo, command.ref)
            files: list[dict[str, Any]] = []
            errors: list[dict[str, str]] = []
            for relative_path in unique:
                try:
                    result = self.reader.read_at_snapshot(
                        command.repo_id,
                        repo,
                        snapshot,
                        relative_path,
                        command.start_line,
                        command.end_line,
                    )
                    files.append(
                        {
                            "repo_id": result.repo_id,
                            "resolved_ref": result.resolved_ref,
                            "commit_sha": result.commit_sha,
                            "path": result.path,
                            "sha256": result.sha256,
                            "size_bytes": result.size_bytes,
                            "total_lines": result.total_lines,
                            "start_line": result.start_line,
                            "end_line": result.end_line,
                            "content": result.content,
                            "truncated": result.truncated,
                        }
                    )
                except (RepoForgeError, ValueError) as exc:
                    error_code = (
                        exc.code.value
                        if isinstance(exc, RepoForgeError)
                        else ErrorCode.INPUT_REQUIRED.value
                    )
                    errors.append(
                        {
                            "path": relative_path,
                            "error_code": error_code,
                            "error_type": type(exc).__name__,
                            "message": str(exc),
                        }
                    )
            return RepositoryFilesReadResult(
                command.repo_id,
                snapshot.resolved_ref,
                snapshot.commit_sha,
                files,
                errors,
                len(unique),
                len(files),
            )

        return self.ctx.audited(
            "repo_read_files",
            {
                "repo_id": command.repo_id,
                "ref": command.ref,
                "file_count": len(unique),
            },
            op,
        )
