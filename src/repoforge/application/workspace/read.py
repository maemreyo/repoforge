"""Unified workspace batch reader with exact-tree cursor binding."""

from __future__ import annotations

from dataclasses import dataclass

from ...domain.errors import SecurityError, WorkspaceError
from ...domain.policy import assert_path_allowed, resolve_workspace_path
from ..context import ApplicationContext
from ..fingerprint_cache import read_fingerprint
from ..read_batch import (
    FileReadRequest,
    LoadedTextFile,
    ReadFileError,
    ReadFileResult,
    execute_batch_read,
)


@dataclass(frozen=True, slots=True)
class WorkspaceReadCommand:
    workspace_id: str
    files: tuple[FileReadRequest, ...]
    byte_budget: int = 60_000
    cursor: str | None = None


@dataclass(frozen=True, slots=True)
class WorkspaceReadResult:
    workspace_id: str
    files: tuple[ReadFileResult, ...]
    errors: tuple[ReadFileError, ...]
    requested: int
    succeeded: int
    truncated: bool
    next_cursor: str | None
    head_sha: str
    workspace_fingerprint: str


class WorkspaceReader:
    def __init__(self, ctx: ApplicationContext):
        self.ctx = ctx

    def execute(self, command: WorkspaceReadCommand) -> WorkspaceReadResult:
        _, repo, workspace = self.ctx.workspace(command.workspace_id)

        def operation() -> WorkspaceReadResult:
            fingerprint = read_fingerprint(
                self.ctx.fingerprint_cache,
                command.workspace_id,
                self.ctx.git,
                workspace,
            ).fingerprint
            head_sha = self.ctx.git.head_sha(workspace)

            def load(raw: str) -> LoadedTextFile:
                normalized = assert_path_allowed(raw, repo)
                unresolved = workspace / normalized
                if self.ctx.filesystem.is_symlink(unresolved):
                    raise SecurityError(f"Reading symlink files is not allowed: {normalized}")
                path = resolve_workspace_path(workspace, normalized, repo)
                if not self.ctx.filesystem.is_file(path):
                    raise WorkspaceError(f"File not found: {normalized}")
                size = self.ctx.filesystem.size(path)
                if size > self.ctx.config.server.max_file_bytes:
                    raise SecurityError(f"File exceeds max_file_bytes: {normalized}")
                return LoadedTextFile(normalized, self.ctx.filesystem.read_bytes(path))

            batch = execute_batch_read(
                kind="workspace_read",
                scope=f"{command.workspace_id}:{head_sha}:{fingerprint}",
                requests=command.files,
                loader=load,
                byte_budget=command.byte_budget,
                cursor=command.cursor,
            )
            if self.ctx.nudge_tracker is not None:
                self.ctx.nudge_tracker.reset_file_reads(command.workspace_id)
            return WorkspaceReadResult(
                workspace_id=command.workspace_id,
                files=batch.files,
                errors=batch.errors,
                requested=batch.requested,
                succeeded=batch.succeeded,
                truncated=batch.truncated,
                next_cursor=batch.next_cursor,
                head_sha=head_sha,
                workspace_fingerprint=fingerprint,
            )

        return self.ctx.audited(
            "workspace_read",
            {
                "workspace_id": command.workspace_id,
                "file_count": len(command.files),
                "byte_budget": command.byte_budget,
                "resumed": command.cursor is not None,
            },
            operation,
        )
