from __future__ import annotations

import hashlib
from dataclasses import dataclass

from ...config import RepositoryConfig
from ...domain.errors import SecurityError
from ...ports.git import ResolvedRepositoryRef
from ..context import ApplicationContext


@dataclass(frozen=True, slots=True)
class RepositoryFileReadCommand:
    repo_id: str
    relative_path: str
    ref: str | None = None
    start_line: int = 1
    end_line: int = 500


@dataclass(frozen=True, slots=True)
class RepositoryFileReadResult:
    repo_id: str
    resolved_ref: str
    commit_sha: str
    path: str
    sha256: str
    size_bytes: int
    total_lines: int
    start_line: int
    end_line: int
    content: str
    truncated: bool


class RepositoryFileReader:
    def __init__(self, ctx: ApplicationContext):
        self.ctx = ctx

    @staticmethod
    def _bound(text: str, limit: int) -> tuple[str, bool]:
        if len(text) <= limit:
            return text, False
        half = max(1, limit // 2)
        omitted = len(text) - (half * 2)
        return (
            f"{text[:half]}\n\n... <{omitted} characters omitted> ...\n\n{text[-half:]}",
            True,
        )

    def read_at_snapshot(
        self,
        repo_id: str,
        repo: RepositoryConfig,
        snapshot: ResolvedRepositoryRef,
        relative_path: str,
        start_line: int,
        end_line: int,
    ) -> RepositoryFileReadResult:
        start = max(1, start_line)
        end = max(start, min(end_line, start + 2000))
        blob = self.ctx.git.read_snapshot_blob(
            repo.path,
            repo,
            snapshot.commit_sha,
            relative_path,
        )
        if b"\x00" in blob.data:
            raise SecurityError("Binary files are not supported by this tool")
        try:
            text = blob.data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SecurityError("File is not valid UTF-8") from exc
        lines = text.splitlines()
        selected = lines[start - 1 : end]
        numbered = "\n".join(
            f"{line_number}: {line}" for line_number, line in enumerate(selected, start=start)
        )
        content, truncated = self._bound(
            numbered,
            self.ctx.config.server.max_tool_output_chars,
        )
        return RepositoryFileReadResult(
            repo_id,
            snapshot.resolved_ref,
            snapshot.commit_sha,
            blob.path,
            hashlib.sha256(blob.data).hexdigest(),
            blob.size_bytes,
            len(lines),
            start,
            min(end, len(lines)),
            content,
            truncated,
        )

    def execute(self, command: RepositoryFileReadCommand) -> RepositoryFileReadResult:
        repo = self.ctx.repo(command.repo_id)

        def op() -> RepositoryFileReadResult:
            snapshot = self.ctx.git.resolve_snapshot_ref(repo.path, repo, command.ref)
            return self.read_at_snapshot(
                command.repo_id,
                repo,
                snapshot,
                command.relative_path,
                command.start_line,
                command.end_line,
            )

        return self.ctx.audited(
            "repo_read_file",
            {
                "repo_id": command.repo_id,
                "ref": command.ref,
                "path": command.relative_path,
            },
            op,
        )
