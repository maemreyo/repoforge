from __future__ import annotations
import hashlib
import re
from dataclasses import dataclass
from ..context import ApplicationContext
from ...domain.errors import SecurityError, WorkspaceError
from ...domain.policy import assert_path_allowed, resolve_workspace_path

_SHA = re.compile("^[a-f0-9]{64}$")


@dataclass(frozen=True, slots=True)
class WorkspaceReplaceTextCommand:
    workspace_id: str
    relative_path: str
    old_text: str
    new_text: str
    expected_sha256: str
    expected_occurrences: int = 1


@dataclass(frozen=True, slots=True)
class WorkspaceReplaceTextResult:
    workspace_id: str
    path: str
    sha256: str
    replacements: int
    diff_stat: str


class WorkspaceTextReplacer:
    def __init__(self, ctx: ApplicationContext):
        self.ctx = ctx

    def execute(self, c: WorkspaceReplaceTextCommand) -> WorkspaceReplaceTextResult:
        if not c.old_text:
            raise ValueError("old_text must be non-empty")
        if "\x00" in c.old_text or "\x00" in c.new_text:
            raise SecurityError("NUL bytes are not allowed in text replacements")
        if c.expected_occurrences <= 0 or c.expected_occurrences > 1000:
            raise ValueError("expected_occurrences must be between 1 and 1000")
        if not _SHA.fullmatch(c.expected_sha256):
            raise ValueError("expected_sha256 must be a lowercase SHA-256")
        _, repo, workspace = self.ctx.workspace(c.workspace_id)
        normalized = assert_path_allowed(c.relative_path, repo)
        path = resolve_workspace_path(workspace, c.relative_path, repo)
        if self.ctx.filesystem.is_symlink(workspace / normalized):
            raise SecurityError("Reading through symlinks is not allowed")

        def op() -> WorkspaceReplaceTextResult:
            with self.ctx.store.lock(c.workspace_id):
                if not self.ctx.filesystem.is_file(
                    path
                ) or self.ctx.filesystem.is_symlink(path):
                    raise WorkspaceError("Target must be an existing regular file")
                data = self.ctx.filesystem.read_bytes(path)
                if b"\x00" in data:
                    raise SecurityError("Binary files are not supported by this tool")
                if len(data) > self.ctx.config.server.max_file_bytes:
                    raise SecurityError("File exceeds max_file_bytes")
                actual = hashlib.sha256(data).hexdigest()
                if actual != c.expected_sha256:
                    raise WorkspaceError(
                        f"File changed since it was read: expected {c.expected_sha256}, got {actual}"
                    )
                try:
                    text = data.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise SecurityError("File is not valid UTF-8") from exc
                count = text.count(c.old_text)
                if count != c.expected_occurrences:
                    raise WorkspaceError(
                        f"Expected {c.expected_occurrences} occurrences, found {count}; no changes applied"
                    )
                encoded = text.replace(
                    c.old_text, c.new_text, c.expected_occurrences
                ).encode("utf-8")
                if len(encoded) > self.ctx.config.server.max_file_bytes:
                    raise SecurityError("Updated content exceeds max_file_bytes")
                self.ctx.filesystem.write_bytes_atomic(
                    path, encoded, preserve_mode=True
                )
                sha = hashlib.sha256(encoded).hexdigest()
                stat = self.ctx.git.diff_stat(workspace)
                return WorkspaceReplaceTextResult(
                    c.workspace_id, normalized, sha, c.expected_occurrences, stat
                )

        return self.ctx.audited(
            "workspace_replace_text",
            {"workspace_id": c.workspace_id, "path": c.relative_path},
            op,
        )
