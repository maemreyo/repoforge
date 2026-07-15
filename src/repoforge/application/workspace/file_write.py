from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from ...domain.errors import SecurityError, WorkspaceError
from ...domain.policy import assert_path_allowed, resolve_workspace_path
from ..context import ApplicationContext
from ..fingerprint_cache import prime_fingerprint

_SHA = re.compile("^[a-f0-9]{64}$")


@dataclass(frozen=True, slots=True)
class WorkspaceFileWriteCommand:
    workspace_id: str
    relative_path: str
    content: str
    expected_sha256: str


@dataclass(frozen=True, slots=True)
class WorkspaceFileWriteResult:
    workspace_id: str
    path: str
    sha256: str
    size_bytes: int
    diff_stat: str
    workspace_fingerprint: str
    head_sha: str


class WorkspaceFileWriter:
    def __init__(self, ctx: ApplicationContext):
        self.ctx = ctx

    def execute(self, c: WorkspaceFileWriteCommand) -> WorkspaceFileWriteResult:
        _, repo, workspace = self.ctx.workspace(c.workspace_id)
        normalized = assert_path_allowed(c.relative_path, repo)
        path = resolve_workspace_path(workspace, c.relative_path, repo)
        data = c.content.encode("utf-8")
        if "\x00" in c.content:
            raise SecurityError("NUL bytes are not allowed in text files")
        if len(data) > self.ctx.config.server.max_file_bytes:
            raise SecurityError("New file content exceeds max_file_bytes")
        if c.expected_sha256 != "<new>" and (not _SHA.fullmatch(c.expected_sha256)):
            raise ValueError("expected_sha256 must be a lowercase SHA-256 or '<new>'")
        if self.ctx.filesystem.is_symlink(workspace / normalized):
            raise SecurityError("Writing through symlinks is not allowed")

        def op() -> WorkspaceFileWriteResult:
            with self.ctx.locks.lock(c.workspace_id):
                if self.ctx.filesystem.exists(path):
                    if self.ctx.filesystem.is_symlink(path) or not self.ctx.filesystem.is_file(
                        path
                    ):
                        raise SecurityError("Only regular files can be overwritten")
                    if c.expected_sha256 == "<new>":
                        raise WorkspaceError("File already exists; supply its current SHA-256")
                    actual = hashlib.sha256(self.ctx.filesystem.read_bytes(path)).hexdigest()
                    if actual != c.expected_sha256:
                        raise WorkspaceError(
                            f"File changed since it was read: expected {c.expected_sha256}, got {actual}"
                        )
                elif c.expected_sha256 != "<new>":
                    raise WorkspaceError(
                        "File does not exist; use expected_sha256='<new>' to create it"
                    )
                self.ctx.filesystem.write_bytes_atomic(path, data, preserve_mode=True)
                sha = hashlib.sha256(data).hexdigest()
                stat = self.ctx.git.diff_stat(workspace)
                fingerprint = prime_fingerprint(
                    self.ctx.fingerprint_cache,
                    c.workspace_id,
                    self.ctx.git,
                    workspace,
                ).fingerprint
                head_sha = self.ctx.git.head_sha(workspace)
                return WorkspaceFileWriteResult(
                    c.workspace_id, normalized, sha, len(data), stat, fingerprint, head_sha
                )

        return self.ctx.audited(
            "workspace_write_file",
            {
                "workspace_id": c.workspace_id,
                "path": c.relative_path,
                "size_bytes": len(data),
            },
            op,
        )
