from __future__ import annotations

import hashlib
from dataclasses import dataclass

from ...domain.errors import SecurityError, WorkspaceError
from ...domain.policy import assert_path_allowed, resolve_workspace_path
from ..context import ApplicationContext


@dataclass(frozen=True, slots=True)
class WorkspaceFileReadCommand:
    workspace_id: str
    relative_path: str
    start_line: int = 1
    end_line: int = 500


@dataclass(frozen=True, slots=True)
class WorkspaceFileReadResult:
    workspace_id: str
    path: str
    sha256: str
    size_bytes: int
    total_lines: int
    start_line: int
    end_line: int
    content: str
    truncated: bool


class WorkspaceFileReader:
    def __init__(self, ctx: ApplicationContext):
        self.ctx = ctx

    @staticmethod
    def _bound(text: str, limit: int) -> tuple[str, bool]:
        if len(text) <= limit:
            return (text, False)
        half = max(1, limit // 2)
        return (
            f"{text[:half]}\n\n... <{len(text) - 2 * half} characters omitted> ...\n\n{text[-half:]}",
            True,
        )

    def execute(self, c: WorkspaceFileReadCommand) -> WorkspaceFileReadResult:
        _, repo, workspace = self.ctx.workspace(c.workspace_id)
        normalized = assert_path_allowed(c.relative_path, repo)
        unresolved = workspace / normalized
        if self.ctx.filesystem.is_symlink(unresolved):
            raise SecurityError("Reading symlink files is not allowed")
        path = resolve_workspace_path(workspace, c.relative_path, repo)
        start = max(1, c.start_line)
        end = max(start, min(c.end_line, start + 2000))

        def op() -> WorkspaceFileReadResult:
            if not self.ctx.filesystem.is_file(path):
                raise WorkspaceError(f"File not found: {c.relative_path}")
            size = self.ctx.filesystem.size(path)
            if size > self.ctx.config.server.max_file_bytes:
                raise SecurityError(
                    f"File size {size} exceeds max_file_bytes={self.ctx.config.server.max_file_bytes}"
                )
            data = self.ctx.filesystem.read_bytes(path)
            if b"\x00" in data:
                raise SecurityError("Binary files are not supported by this tool")
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise SecurityError("File is not valid UTF-8") from exc
            lines = text.splitlines()
            selected = lines[start - 1 : end]
            numbered = "\n".join((f"{n}: {line}" for n, line in enumerate(selected, start=start)))
            content, truncated = self._bound(numbered, self.ctx.config.server.max_tool_output_chars)
            return WorkspaceFileReadResult(
                c.workspace_id,
                normalized,
                hashlib.sha256(data).hexdigest(),
                size,
                len(lines),
                start,
                min(end, len(lines)),
                content,
                truncated,
            )

        return self.ctx.audited(
            "workspace_read_file",
            {"workspace_id": c.workspace_id, "path": c.relative_path},
            op,
        )
