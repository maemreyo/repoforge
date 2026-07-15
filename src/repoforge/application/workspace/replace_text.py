from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from ...domain.errors import SecurityError, WorkspaceError
from ...domain.policy import assert_path_allowed, resolve_workspace_path
from ..context import ApplicationContext
from ..fingerprint_cache import prime_fingerprint

_SHA = re.compile("^[a-f0-9]{64}$")

#: Hard ceiling on how many ordered edits one `workspace_replace_text` call may batch
#: against a single file. Audit evidence (issue #142) shows real consecutive-edit runs
#: top out at 14; 20 leaves headroom without letting one call become an unbounded patch.
MAX_EDITS_PER_CALL = 20


@dataclass(frozen=True, slots=True)
class TextEdit:
    """One ordered exact-replacement entry within a batched `workspace_replace_text` call."""

    old_text: str
    new_text: str
    expected_occurrences: int = 1


@dataclass(frozen=True, slots=True)
class WorkspaceReplaceTextCommand:
    workspace_id: str
    relative_path: str
    expected_sha256: str
    old_text: str | None = None
    new_text: str | None = None
    expected_occurrences: int = 1
    edits: tuple[TextEdit, ...] | None = None


@dataclass(frozen=True, slots=True)
class WorkspaceReplaceTextResult:
    workspace_id: str
    path: str
    sha256: str
    replacements: int
    diff_stat: str
    workspace_fingerprint: str
    head_sha: str


@dataclass(frozen=True, slots=True)
class WorkspaceReplaceTextEditOutcome:
    """Per-entry replacement count for a batched `workspace_replace_text` call."""

    index: int
    replacements: int


@dataclass(frozen=True, slots=True)
class WorkspaceReplaceTextBatchResult:
    workspace_id: str
    path: str
    sha256: str
    replacements: int
    diff_stat: str
    workspace_fingerprint: str
    head_sha: str
    edits: tuple[WorkspaceReplaceTextEditOutcome, ...]


class WorkspaceTextReplacer:
    def __init__(self, ctx: ApplicationContext):
        self.ctx = ctx

    def execute(
        self, c: WorkspaceReplaceTextCommand
    ) -> WorkspaceReplaceTextResult | WorkspaceReplaceTextBatchResult:
        if c.edits is not None:
            return self._execute_batch(c)
        return self._execute_single(c)

    def _resolve_target(self, c: WorkspaceReplaceTextCommand) -> tuple[Path, Path, str]:
        _, repo, workspace = self.ctx.workspace(c.workspace_id)
        normalized = assert_path_allowed(c.relative_path, repo)
        path = resolve_workspace_path(workspace, c.relative_path, repo)
        if self.ctx.filesystem.is_symlink(workspace / normalized):
            raise SecurityError("Reading through symlinks is not allowed")
        return workspace, path, normalized

    def _read_verified_text(self, path: Path, expected_sha256: str) -> str:
        if not self.ctx.filesystem.is_file(path) or self.ctx.filesystem.is_symlink(path):
            raise WorkspaceError("Target must be an existing regular file")
        data = self.ctx.filesystem.read_bytes(path)
        if b"\x00" in data:
            raise SecurityError("Binary files are not supported by this tool")
        if len(data) > self.ctx.config.server.max_file_bytes:
            raise SecurityError("File exceeds max_file_bytes")
        actual = hashlib.sha256(data).hexdigest()
        if actual != expected_sha256:
            raise WorkspaceError(
                f"File changed since it was read: expected {expected_sha256}, got {actual}"
            )
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SecurityError("File is not valid UTF-8") from exc

    def _execute_single(self, c: WorkspaceReplaceTextCommand) -> WorkspaceReplaceTextResult:
        old_text = c.old_text or ""
        new_text = c.new_text if c.new_text is not None else ""
        if not old_text:
            raise ValueError("old_text must be non-empty")
        if "\x00" in old_text or "\x00" in new_text:
            raise SecurityError("NUL bytes are not allowed in text replacements")
        if c.expected_occurrences <= 0 or c.expected_occurrences > 1000:
            raise ValueError("expected_occurrences must be between 1 and 1000")
        if not _SHA.fullmatch(c.expected_sha256):
            raise ValueError("expected_sha256 must be a lowercase SHA-256")
        workspace, path, normalized = self._resolve_target(c)

        def op() -> WorkspaceReplaceTextResult:
            with self.ctx.locks.lock(c.workspace_id):
                text = self._read_verified_text(path, c.expected_sha256)
                count = text.count(old_text)
                if count != c.expected_occurrences:
                    raise WorkspaceError(
                        f"Expected {c.expected_occurrences} occurrences, found {count}; "
                        "no changes applied"
                    )
                encoded = text.replace(old_text, new_text, c.expected_occurrences).encode("utf-8")
                if len(encoded) > self.ctx.config.server.max_file_bytes:
                    raise SecurityError("Updated content exceeds max_file_bytes")
                self.ctx.filesystem.write_bytes_atomic(path, encoded, preserve_mode=True)
                sha = hashlib.sha256(encoded).hexdigest()
                stat = self.ctx.git.diff_stat(workspace)
                fingerprint = prime_fingerprint(
                    self.ctx.fingerprint_cache,
                    c.workspace_id,
                    self.ctx.git,
                    workspace,
                ).fingerprint
                head_sha = self.ctx.git.head_sha(workspace)
                return WorkspaceReplaceTextResult(
                    c.workspace_id,
                    normalized,
                    sha,
                    c.expected_occurrences,
                    stat,
                    fingerprint,
                    head_sha,
                )

        return self.ctx.audited(
            "workspace_replace_text",
            {"workspace_id": c.workspace_id, "path": c.relative_path},
            op,
        )

    def _execute_batch(self, c: WorkspaceReplaceTextCommand) -> WorkspaceReplaceTextBatchResult:
        entries = list(c.edits or ())
        if not entries:
            raise ValueError("edits must contain at least one entry")
        if len(entries) > MAX_EDITS_PER_CALL:
            raise ValueError(
                f"edits must contain at most {MAX_EDITS_PER_CALL} entries, got {len(entries)}"
            )
        if c.old_text is not None or c.new_text is not None:
            raise ValueError(
                "old_text and new_text must not be provided together with edits; "
                "use edits for every replacement in a batched call"
            )
        for index, entry in enumerate(entries):
            if not entry.old_text:
                raise ValueError(f"edits[{index}]: old_text must be non-empty")
            if "\x00" in entry.old_text or "\x00" in entry.new_text:
                raise SecurityError(
                    f"edits[{index}]: NUL bytes are not allowed in text replacements"
                )
            if entry.expected_occurrences <= 0 or entry.expected_occurrences > 1000:
                raise ValueError(f"edits[{index}]: expected_occurrences must be between 1 and 1000")
        if not _SHA.fullmatch(c.expected_sha256):
            raise ValueError("expected_sha256 must be a lowercase SHA-256")
        workspace, path, normalized = self._resolve_target(c)

        def op() -> WorkspaceReplaceTextBatchResult:
            with self.ctx.locks.lock(c.workspace_id):
                buffer = self._read_verified_text(path, c.expected_sha256)
                outcomes: list[WorkspaceReplaceTextEditOutcome] = []
                for index, entry in enumerate(entries):
                    count = buffer.count(entry.old_text)
                    if count != entry.expected_occurrences:
                        raise WorkspaceError(
                            f"edits[{index}]: expected {entry.expected_occurrences} occurrences, "
                            f"found {count}; no changes applied"
                        )
                    buffer = buffer.replace(
                        entry.old_text, entry.new_text, entry.expected_occurrences
                    )
                    outcomes.append(
                        WorkspaceReplaceTextEditOutcome(index, entry.expected_occurrences)
                    )
                encoded = buffer.encode("utf-8")
                if len(encoded) > self.ctx.config.server.max_file_bytes:
                    raise SecurityError("Updated content exceeds max_file_bytes")
                self.ctx.filesystem.write_bytes_atomic(path, encoded, preserve_mode=True)
                sha = hashlib.sha256(encoded).hexdigest()
                stat = self.ctx.git.diff_stat(workspace)
                fingerprint = prime_fingerprint(
                    self.ctx.fingerprint_cache,
                    c.workspace_id,
                    self.ctx.git,
                    workspace,
                ).fingerprint
                head_sha = self.ctx.git.head_sha(workspace)
                return WorkspaceReplaceTextBatchResult(
                    c.workspace_id,
                    normalized,
                    sha,
                    sum(outcome.replacements for outcome in outcomes),
                    stat,
                    fingerprint,
                    head_sha,
                    tuple(outcomes),
                )

        return self.ctx.audited(
            "workspace_replace_text",
            {"workspace_id": c.workspace_id, "path": c.relative_path},
            op,
        )
