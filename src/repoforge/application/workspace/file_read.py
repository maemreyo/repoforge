from __future__ import annotations

import hashlib
from dataclasses import dataclass

from ...domain.errors import SecurityError, WorkspaceError
from ...domain.policy import assert_path_allowed, resolve_workspace_path
from ..context import ApplicationContext

_DEFAULT_NEXT_STEP = "Continue reading or editing files as needed."


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
    next_step: str = _DEFAULT_NEXT_STEP


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
            next_step = _DEFAULT_NEXT_STEP
            tracker = self.ctx.nudge_tracker
            if tracker is not None and tracker.observe_single_file_read(
                c.workspace_id, self.ctx.now_epoch()
            ):
                next_step = (
                    "Recent reads have repeatedly targeted this workspace one file at a time; "
                    f'call workspace_read_files(workspace_id="{c.workspace_id}", '
                    'relative_paths=[...]) to batch several files in one call instead of '
                    "repeating workspace_read_file."
                )
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
                next_step,
            )

        return self.ctx.audited(
            "workspace_read_file",
            {"workspace_id": c.workspace_id, "path": c.relative_path},
            op,
        )
