"""Typed application use case for reading files in isolated Git workspaces."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from .config import RepositoryConfig
from .errors import SecurityError, WorkspaceError
from .security import assert_path_allowed, resolve_workspace_path


@dataclass(frozen=True, slots=True)
class WorkspaceFileReadCommand:
    """Validated caller intent to read one file in a registered workspace."""

    workspace_id: str
    relative_path: str
    start_line: int = 1
    end_line: int = 500


@dataclass(frozen=True, slots=True)
class WorkspaceFileReadResult:
    """Read file content, identity, and line range."""

    workspace_id: str
    path: str
    sha256: str
    size_bytes: int
    total_lines: int
    start_line: int
    end_line: int
    content: str
    truncated: bool


@dataclass(frozen=True, slots=True)
class WorkspaceFileReadPorts:
    """Constrained adapters required to read a file in a workspace."""

    max_file_bytes: int
    max_tool_output_chars: int


def _bounded_text(text: str, limit: int) -> tuple[str, bool]:
    """Truncate text in the middle when it exceeds the character limit."""
    if len(text) <= limit:
        return text, False
    half = max(1, limit // 2)
    omitted = len(text) - (half * 2)
    bounded = f"{text[:half]}\n\n... <{omitted} characters omitted> ...\n\n{text[-half:]}"
    return bounded, True


class WorkspaceFileReader:
    """Reads one file from a registered workspace with safety checks."""

    def __init__(self, ports: WorkspaceFileReadPorts) -> None:
        self._ports: WorkspaceFileReadPorts = ports

    @property
    def max_file_bytes(self) -> int:
        """Maximum allowed file size for reading, in bytes."""
        return self._ports.max_file_bytes

    @property
    def max_tool_output_chars(self) -> int:
        """Maximum allowed output characters before truncation."""
        return self._ports.max_tool_output_chars

    def execute(
        self,
        repo: RepositoryConfig,
        workspace_path: Path,
        command: WorkspaceFileReadCommand,
    ) -> WorkspaceFileReadResult:
        normalized_path = assert_path_allowed(command.relative_path, repo)
        # Symlink check on the unresolved path — resolve_workspace_path follows them.
        if (workspace_path / normalized_path).is_symlink():
            raise SecurityError("Reading symlink files is not allowed")

        file_path = resolve_workspace_path(workspace_path, command.relative_path, repo)

        if not file_path.is_file():
            raise WorkspaceError(f"File not found: {command.relative_path}")

        size = file_path.stat().st_size
        if size > self._ports.max_file_bytes:
            raise SecurityError(
                f"File size {size} exceeds max_file_bytes={self._ports.max_file_bytes}"
            )

        data = file_path.read_bytes()
        if b"\x00" in data:
            raise SecurityError("Binary files are not supported by this tool")

        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SecurityError("File is not valid UTF-8") from exc

        start_line = max(1, command.start_line)
        end_line = max(start_line, min(command.end_line, start_line + 2000))
        lines = text.splitlines()
        selected = lines[start_line - 1 : end_line]
        numbered = "\n".join(
            f"{line_number}: {line}" for line_number, line in enumerate(selected, start=start_line)
        )
        bounded_content, truncated = _bounded_text(numbered, self._ports.max_tool_output_chars)

        return WorkspaceFileReadResult(
            workspace_id=command.workspace_id,
            path=normalized_path,
            sha256=hashlib.sha256(data).hexdigest(),
            size_bytes=size,
            total_lines=len(lines),
            start_line=start_line,
            end_line=min(end_line, len(lines)),
            content=bounded_content,
            truncated=truncated,
        )
