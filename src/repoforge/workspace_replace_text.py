"""Typed application use case for text replacement in files within isolated Git workspaces."""

from __future__ import annotations

import hashlib
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

from .config import RepositoryConfig
from .errors import SecurityError, WorkspaceError
from .ports import CommandExecutor, WorkspaceStore
from .security import assert_path_allowed, resolve_workspace_path

_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")


@dataclass(frozen=True, slots=True)
class WorkspaceReplaceTextCommand:
    """Validated caller intent to replace exact text in one file within a registered workspace."""

    workspace_id: str
    relative_path: str
    old_text: str
    new_text: str
    expected_sha256: str
    expected_occurrences: int = 1


@dataclass(frozen=True, slots=True)
class WorkspaceReplaceTextResult:
    """Replaced text identity, hash, replacement count, and workspace diff statistic."""

    workspace_id: str
    path: str
    sha256: str
    replacements: int
    diff_stat: str


@dataclass(frozen=True, slots=True)
class WorkspaceReplaceTextPorts:
    """Constrained adapters required to replace text in a workspace file."""

    state: WorkspaceStore
    runner: CommandExecutor
    max_file_bytes: int


class WorkspaceTextReplacer:
    """Replaces exact text occurrences in one workspace file with optimistic locking."""

    def __init__(self, ports: WorkspaceReplaceTextPorts) -> None:
        self._ports: WorkspaceReplaceTextPorts = ports

    @property
    def max_file_bytes(self) -> int:
        """Maximum allowed file size for the target file and resulting content, in bytes."""
        return self._ports.max_file_bytes

    def execute(
        self,
        repo: RepositoryConfig,
        workspace_path: Path,
        command: WorkspaceReplaceTextCommand,
    ) -> WorkspaceReplaceTextResult:
        """Validate inputs, lock the workspace, and atomically replace text in the file."""
        # -- Input validation (pre-lock, deterministic) --
        if not command.old_text:
            raise ValueError("old_text must be non-empty")
        if "\x00" in command.old_text or "\x00" in command.new_text:
            raise SecurityError("NUL bytes are not allowed in text replacements")
        if command.expected_occurrences <= 0 or command.expected_occurrences > 1000:
            raise ValueError("expected_occurrences must be between 1 and 1000")
        if not _SHA256_RE.fullmatch(command.expected_sha256):
            raise ValueError("expected_sha256 must be a lowercase SHA-256")

        file_path = resolve_workspace_path(workspace_path, command.relative_path, repo)

        # Reject symlinks on the unresolved path — resolve_workspace_path follows them via
        # .resolve(), so the unresolved entry must be checked explicitly before entering
        # the lock.
        if (workspace_path / assert_path_allowed(command.relative_path, repo)).is_symlink():
            raise SecurityError("Reading through symlinks is not allowed")

        # -- Lock and replace --
        with self._ports.state.lock(command.workspace_id):
            if not file_path.is_file() or file_path.is_symlink():
                raise WorkspaceError("Target must be an existing regular file")

            data = file_path.read_bytes()
            if b"\x00" in data:
                raise SecurityError("Binary files are not supported by this tool")
            if len(data) > self._ports.max_file_bytes:
                raise SecurityError("File exceeds max_file_bytes")

            actual_sha = hashlib.sha256(data).hexdigest()
            if actual_sha != command.expected_sha256:
                raise WorkspaceError(
                    f"File changed since it was read: expected {command.expected_sha256}, got {actual_sha}"
                )

            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise SecurityError("File is not valid UTF-8") from exc
            count = text.count(command.old_text)
            if count != command.expected_occurrences:
                raise WorkspaceError(
                    f"Expected {command.expected_occurrences} occurrences, found {count}; no changes applied"
                )

            updated = text.replace(command.old_text, command.new_text, command.expected_occurrences)
            encoded = updated.encode("utf-8")
            if len(encoded) > self._ports.max_file_bytes:
                raise SecurityError("Updated content exceeds max_file_bytes")

            existing_mode = stat.S_IMODE(file_path.stat().st_mode)
            temporary = file_path.with_name(f".{file_path.name}.rf-{os.getpid()}")
            try:
                _ = temporary.write_bytes(encoded)
                os.chmod(temporary, existing_mode)
                os.replace(temporary, file_path)
            finally:
                temporary.unlink(missing_ok=True)

            new_sha = hashlib.sha256(encoded).hexdigest()
            diff_stat = self._ports.runner.run(
                ["git", "diff", "--stat", "--"],
                cwd=workspace_path,
            ).stdout

            return WorkspaceReplaceTextResult(
                workspace_id=command.workspace_id,
                path=assert_path_allowed(command.relative_path, repo),
                sha256=new_sha,
                replacements=command.expected_occurrences,
                diff_stat=diff_stat,
            )
