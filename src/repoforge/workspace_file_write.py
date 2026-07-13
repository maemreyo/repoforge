"""Typed application use case for writing files in isolated Git workspaces."""

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


def sha256_file(path: Path) -> str:
    """Compute the SHA-256 digest of a file on disk (chunked streaming)."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True, slots=True)
class WorkspaceFileWriteCommand:
    """Validated caller intent to write one file in a registered workspace."""

    workspace_id: str
    relative_path: str
    content: str
    expected_sha256: str


@dataclass(frozen=True, slots=True)
class WorkspaceFileWriteResult:
    """Written file identity, hash, size, and workspace diff statistic."""

    workspace_id: str
    path: str
    sha256: str
    size_bytes: int
    diff_stat: str


@dataclass(frozen=True, slots=True)
class WorkspaceFileWritePorts:
    """Constrained adapters required to write a file in a workspace."""

    state: WorkspaceStore
    runner: CommandExecutor
    max_file_bytes: int


class WorkspaceFileWriter:
    """Writes one file to a registered workspace with optimistic locking."""

    def __init__(self, ports: WorkspaceFileWritePorts) -> None:
        self._ports: WorkspaceFileWritePorts = ports

    @property
    def max_file_bytes(self) -> int:
        """Maximum allowed file size for new content, in bytes."""
        return self._ports.max_file_bytes

    def execute(
        self,
        repo: RepositoryConfig,
        workspace_path: Path,
        command: WorkspaceFileWriteCommand,
    ) -> WorkspaceFileWriteResult:
        """Validate inputs, lock the workspace, and atomically write the file."""
        if "\x00" in command.content:
            raise SecurityError("NUL bytes are not allowed in text files")
        encoded = command.content.encode("utf-8")
        if len(encoded) > self._ports.max_file_bytes:
            raise SecurityError("New file content exceeds max_file_bytes")
        if command.expected_sha256 != "<new>" and not _SHA256_RE.fullmatch(command.expected_sha256):
            raise ValueError("expected_sha256 must be a lowercase SHA-256 or '<new>'")

        file_path = resolve_workspace_path(workspace_path, command.relative_path, repo)

        # Reject symlinks — resolve_workspace_path follows them via .resolve(),
        # so the pre-resolved path must be checked explicitly.
        if (workspace_path / assert_path_allowed(command.relative_path, repo)).is_symlink():
            raise SecurityError("Writing through symlinks is not allowed")

        with self._ports.state.lock(command.workspace_id):
            if file_path.exists():
                if file_path.is_symlink() or not file_path.is_file():
                    raise SecurityError("Only regular files can be overwritten")
                if command.expected_sha256 == "<new>":
                    raise WorkspaceError("File already exists; supply its current SHA-256")
                actual = sha256_file(file_path)
                if actual != command.expected_sha256:
                    raise WorkspaceError(
                        f"File changed since it was read: expected {command.expected_sha256}, got {actual}"
                    )
            elif command.expected_sha256 != "<new>":
                raise WorkspaceError(
                    "File does not exist; use expected_sha256='<new>' to create it"
                )

            existing_mode = stat.S_IMODE(file_path.stat().st_mode) if file_path.exists() else None
            file_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = file_path.with_name(f".{file_path.name}.rf-{os.getpid()}")
            try:
                _ = temporary.write_bytes(encoded)
                if existing_mode is not None:
                    os.chmod(temporary, existing_mode)
                os.replace(temporary, file_path)
            finally:
                temporary.unlink(missing_ok=True)

            new_sha = hashlib.sha256(encoded).hexdigest()
            diff_stat = self._ports.runner.run(
                ["git", "diff", "--stat", "--"], cwd=workspace_path
            ).stdout

            return WorkspaceFileWriteResult(
                workspace_id=command.workspace_id,
                path=assert_path_allowed(command.relative_path, repo),
                sha256=new_sha,
                size_bytes=len(encoded),
                diff_stat=diff_stat,
            )
