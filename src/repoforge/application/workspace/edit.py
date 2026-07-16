from __future__ import annotations

import contextlib
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from ...domain.errors import SecurityError, WorkspaceError
from ...domain.policy import assert_path_allowed, resolve_workspace_path
from ..context import ApplicationContext
from ..fingerprint_cache import prime_fingerprint

_SHA = re.compile("^[a-f0-9]{64}$")

#: Hard ceiling on how many ordered edits one `workspace_edit` call may batch against a
#: single file. Audit evidence (issue #142) shows real consecutive-edit runs top out at
#: 14; 20 leaves headroom without letting one call become an unbounded patch.
MAX_EDITS_PER_FILE = 20

#: Hard ceiling on how many files one `workspace_edit` call may touch. Mirrors
#: MAX_EDITS_PER_FILE's rationale: bound the blast radius of a single call while still
#: covering realistic multi-file changesets.
MAX_FILES_PER_CALL = 20


@dataclass(frozen=True, slots=True)
class TextEdit:
    """One ordered exact-replacement entry within a `workspace_edit` file entry."""

    old_text: str
    new_text: str
    expected_occurrences: int = 1


@dataclass(frozen=True, slots=True)
class FileEdit:
    """One file's ordered edits within a batched `workspace_edit` call."""

    path: str
    expected_sha256: str
    edits: tuple[TextEdit, ...]


@dataclass(frozen=True, slots=True)
class WorkspaceEditCommand:
    workspace_id: str
    files: tuple[FileEdit, ...]


@dataclass(frozen=True, slots=True)
class WorkspaceEditFileResult:
    path: str
    sha256: str
    replacements: int


@dataclass(frozen=True, slots=True)
class WorkspaceEditResult:
    workspace_id: str
    files: tuple[WorkspaceEditFileResult, ...]
    diff_stat: str
    workspace_fingerprint: str
    head_sha: str


@dataclass(frozen=True, slots=True)
class _PreparedFile:
    path: Path
    normalized: str
    original: bytes
    encoded: bytes
    replacements: int


class WorkspaceEditor:
    def __init__(self, ctx: ApplicationContext):
        self.ctx = ctx

    def execute(self, c: WorkspaceEditCommand) -> WorkspaceEditResult:
        files = list(c.files)
        if not files:
            raise ValueError("files must contain at least one entry")
        if len(files) > MAX_FILES_PER_CALL:
            raise ValueError(
                f"files must contain at most {MAX_FILES_PER_CALL} entries, got {len(files)}"
            )
        seen_paths: set[str] = set()
        for index, entry in enumerate(files):
            if not _SHA.fullmatch(entry.expected_sha256):
                raise ValueError(f"files[{index}]: expected_sha256 must be a lowercase SHA-256")
            if entry.path in seen_paths:
                raise ValueError(
                    f"files[{index}]: duplicate path {entry.path!r}; "
                    "consolidate all edits for one file into a single entry"
                )
            seen_paths.add(entry.path)
            edits = list(entry.edits)
            if not edits:
                raise ValueError(f"files[{index}]: edits must contain at least one entry")
            if len(edits) > MAX_EDITS_PER_FILE:
                raise ValueError(
                    f"files[{index}]: edits must contain at most {MAX_EDITS_PER_FILE} entries, "
                    f"got {len(edits)}"
                )
            for edit_index, edit in enumerate(edits):
                if not edit.old_text:
                    raise ValueError(
                        f"files[{index}].edits[{edit_index}]: old_text must be non-empty"
                    )
                if "\x00" in edit.old_text or "\x00" in edit.new_text:
                    raise SecurityError(
                        f"files[{index}].edits[{edit_index}]: NUL bytes are not allowed in text replacements"
                    )
                if edit.expected_occurrences <= 0 or edit.expected_occurrences > 1000:
                    raise ValueError(
                        f"files[{index}].edits[{edit_index}]: expected_occurrences must be between 1 and 1000"
                    )

        _, repo, workspace = self.ctx.workspace(c.workspace_id)
        resolved: list[tuple[Path, str]] = []
        for entry in files:
            normalized = assert_path_allowed(entry.path, repo)
            path = resolve_workspace_path(workspace, entry.path, repo)
            if self.ctx.filesystem.is_symlink(workspace / normalized):
                raise SecurityError("Reading through symlinks is not allowed")
            resolved.append((path, normalized))

        def op() -> WorkspaceEditResult:
            with self.ctx.locks.lock(c.workspace_id):
                prepared = [
                    self._prepare(path, normalized, entry)
                    for (path, normalized), entry in zip(resolved, files, strict=True)
                ]
                self._write_all(prepared)
                stat = self.ctx.git.diff_stat(workspace)
                fingerprint = prime_fingerprint(
                    self.ctx.fingerprint_cache,
                    c.workspace_id,
                    self.ctx.git,
                    workspace,
                ).fingerprint
                head_sha = self.ctx.git.head_sha(workspace)
                return WorkspaceEditResult(
                    c.workspace_id,
                    tuple(
                        WorkspaceEditFileResult(
                            item.normalized,
                            hashlib.sha256(item.encoded).hexdigest(),
                            item.replacements,
                        )
                        for item in prepared
                    ),
                    stat,
                    fingerprint,
                    head_sha,
                )

        return self.ctx.audited(
            "workspace_edit",
            {"workspace_id": c.workspace_id, "paths": [entry.path for entry in files]},
            op,
        )

    def _prepare(self, path: Path, normalized: str, entry: FileEdit) -> _PreparedFile:
        if not self.ctx.filesystem.is_file(path) or self.ctx.filesystem.is_symlink(path):
            raise WorkspaceError(f"Target must be an existing regular file: {normalized}")
        data = self.ctx.filesystem.read_bytes(path)
        if b"\x00" in data:
            raise SecurityError(f"Binary files are not supported by this tool: {normalized}")
        if len(data) > self.ctx.config.server.max_file_bytes:
            raise SecurityError(f"File exceeds max_file_bytes: {normalized}")
        actual = hashlib.sha256(data).hexdigest()
        if actual != entry.expected_sha256:
            raise WorkspaceError(
                f"File changed since it was read: {normalized}: "
                f"expected {entry.expected_sha256}, got {actual}"
            )
        try:
            buffer = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SecurityError(f"File is not valid UTF-8: {normalized}") from exc

        replacements = 0
        for edit_index, edit in enumerate(entry.edits):
            count = buffer.count(edit.old_text)
            if count != edit.expected_occurrences:
                raise WorkspaceError(
                    f"{normalized}: edits[{edit_index}]: expected {edit.expected_occurrences} "
                    f"occurrences, found {count}; no changes applied"
                )
            buffer = buffer.replace(edit.old_text, edit.new_text, edit.expected_occurrences)
            replacements += edit.expected_occurrences
            if len(buffer.encode("utf-8")) > self.ctx.config.server.max_file_bytes:
                raise SecurityError(
                    f"{normalized}: edits[{edit_index}]: intermediate content exceeds "
                    "max_file_bytes"
                )

        encoded = buffer.encode("utf-8")
        return _PreparedFile(path, normalized, data, encoded, replacements)

    def _write_all(self, prepared: list[_PreparedFile]) -> None:
        written: list[_PreparedFile] = []
        try:
            for item in prepared:
                self.ctx.filesystem.write_bytes_atomic(item.path, item.encoded, preserve_mode=True)
                written.append(item)
        except Exception:
            for item in written:
                with contextlib.suppress(Exception):
                    self.ctx.filesystem.write_bytes_atomic(
                        item.path, item.original, preserve_mode=True
                    )
            raise
