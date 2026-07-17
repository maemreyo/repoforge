"""Unified, journaled workspace mutation planning and execution."""

from __future__ import annotations

import hashlib
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import TypeAlias

from ...domain.errors import ConfigError, ErrorCode, RepoForgeError, SecurityError, WorkspaceError
from ...domain.filesystem_transaction import (
    CreateFile,
    DeleteFile,
    MoveFile,
    TransactionAction,
    TransactionPlan,
    WriteFile,
)
from ...domain.patches import materialize_normalized_patch, normalize_patch
from ...domain.policy import assert_path_allowed, resolve_workspace_path, validate_patch
from ..context import ApplicationContext
from ..fingerprint_cache import prime_fingerprint, read_fingerprint

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MAX_OPERATIONS = 100
_MAX_REPLACEMENTS = 20


@dataclass(frozen=True, slots=True)
class TextReplacement:
    old_text: str
    new_text: str
    expected_occurrences: int = 1


@dataclass(frozen=True, slots=True)
class ReplaceTextMutation:
    path: str
    expected_sha256: str
    edits: tuple[TextReplacement, ...]


@dataclass(frozen=True, slots=True)
class WriteMutation:
    path: str
    content: str
    expected_sha256: str
    preserve_mode: bool = True


@dataclass(frozen=True, slots=True)
class CreateMutation:
    path: str
    content: str
    mode: int = 0o644


@dataclass(frozen=True, slots=True)
class DeleteMutation:
    path: str
    expected_sha256: str


@dataclass(frozen=True, slots=True)
class MoveMutation:
    source: str
    destination: str
    expected_source_sha256: str


@dataclass(frozen=True, slots=True)
class ApplyPatchMutation:
    patch: str


@dataclass(frozen=True, slots=True)
class RestoreMutation:
    paths: tuple[str, ...]


WorkspaceMutation: TypeAlias = (
    ReplaceTextMutation
    | WriteMutation
    | CreateMutation
    | DeleteMutation
    | MoveMutation
    | ApplyPatchMutation
    | RestoreMutation
)


@dataclass(frozen=True, slots=True)
class WorkspaceMutateCommand:
    workspace_id: str
    operations: tuple[WorkspaceMutation, ...]
    expected_workspace_fingerprint: str
    dry_run: bool = False


@dataclass(frozen=True, slots=True)
class MutationDiagnostic:
    index: int
    op: str
    path: str | None
    status: str
    changed: bool
    before_sha256: str | None
    after_sha256: str | None
    candidate_context: str | None
    failure_reason: str | None
    repair_actions: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class WorkspaceMutateResult:
    workspace_id: str
    dry_run: bool
    ready: bool
    changed: bool
    would_change: bool
    operation_count: int
    operations: tuple[MutationDiagnostic, ...]
    changed_paths: tuple[str, ...]
    workspace_fingerprint: str
    head_sha: str
    diff_stat: str
    change_metrics: dict[str, object]
    transaction_id: str | None


@dataclass(frozen=True, slots=True)
class _VirtualFile:
    data: bytes
    mode: int

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()


class _MutationPlanner:
    def __init__(
        self,
        ctx: ApplicationContext,
        workspace: Path,
        repo: object,
        head_sha: str,
    ) -> None:
        self.ctx = ctx
        self.workspace = workspace
        self.repo = repo
        self.head_sha = head_sha
        self.original: dict[str, _VirtualFile | None] = {}
        self.current: dict[str, _VirtualFile | None] = {}
        self.move_hints: list[tuple[str, str]] = []

    def normalize(self, raw: str) -> str:
        normalized = assert_path_allowed(raw, self.repo)  # type: ignore[arg-type]
        resolve_workspace_path(self.workspace, normalized, self.repo)  # type: ignore[arg-type]
        return normalized

    def load(self, raw: str) -> tuple[str, _VirtualFile | None]:
        path = self.normalize(raw)
        if path in self.current:
            return path, self.current[path]
        candidate = self.workspace / path
        if candidate.is_symlink():
            raise SecurityError(f"Mutation paths cannot be symlinks: {path}")
        if not candidate.exists():
            value: _VirtualFile | None = None
        else:
            if not candidate.is_file():
                raise SecurityError(f"Mutation target must be a regular file: {path}")
            size = candidate.stat().st_size
            if size > self.ctx.config.server.max_file_bytes:
                raise SecurityError(f"Mutation target exceeds max_file_bytes: {path}")
            value = _VirtualFile(candidate.read_bytes(), stat.S_IMODE(candidate.stat().st_mode))
        self.original[path] = value
        self.current[path] = value
        return path, value

    def set(self, path: str, value: _VirtualFile | None) -> None:
        if path not in self.original:
            self.load(path)
        self.current[path] = value

    def require_existing(self, raw: str) -> tuple[str, _VirtualFile]:
        path, value = self.load(raw)
        if value is None:
            raise WorkspaceError(f"Mutation target does not exist: {path}")
        return path, value

    def require_absent(self, raw: str) -> str:
        path, value = self.load(raw)
        if value is not None:
            raise WorkspaceError(f"Mutation create target already exists: {path}")
        return path

    def validate_sha(self, supplied: str, actual: str, *, path: str) -> None:
        if _SHA256.fullmatch(supplied) is None:
            raise ValueError("expected_sha256 must be a lowercase SHA-256")
        if supplied != actual:
            raise WorkspaceError(
                f"expected_sha256 mismatch for {path}: expected {supplied}, got {actual}"
            )

    def text_bytes(self, content: str, *, path: str) -> bytes:
        if "\x00" in content:
            raise SecurityError(f"NUL bytes are not allowed in text mutation content: {path}")
        encoded = content.encode("utf-8")
        if len(encoded) > self.ctx.config.server.max_file_bytes:
            raise SecurityError(f"Mutation content exceeds max_file_bytes: {path}")
        return encoded

    def text(self, value: _VirtualFile, *, path: str) -> str:
        if b"\x00" in value.data:
            raise SecurityError(f"Mutation target is binary and cannot be edited as text: {path}")
        try:
            return value.data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SecurityError(f"Mutation target is not valid UTF-8 text: {path}") from exc

    def apply(self, operation: WorkspaceMutation) -> MutationDiagnostic:
        if isinstance(operation, ReplaceTextMutation):
            return self._replace(operation)
        if isinstance(operation, WriteMutation):
            return self._write(operation)
        if isinstance(operation, CreateMutation):
            return self._create(operation)
        if isinstance(operation, DeleteMutation):
            return self._delete(operation)
        if isinstance(operation, MoveMutation):
            return self._move(operation)
        if isinstance(operation, ApplyPatchMutation):
            return self._patch(operation)
        return self._restore(operation)

    def _replace(self, operation: ReplaceTextMutation) -> MutationDiagnostic:
        path, existing = self.require_existing(operation.path)
        self.validate_sha(operation.expected_sha256, existing.sha256, path=path)
        if not operation.edits or len(operation.edits) > _MAX_REPLACEMENTS:
            raise ValueError(
                f"replace_text edits must contain between 1 and {_MAX_REPLACEMENTS} entries"
            )
        content = self.text(existing, path=path)
        before = existing.sha256
        replacements = 0
        for index, edit in enumerate(operation.edits):
            if not edit.old_text:
                raise ValueError(f"edits[{index}].old_text must be non-empty")
            if "\x00" in edit.old_text or "\x00" in edit.new_text:
                raise SecurityError("NUL bytes are not allowed in text replacements")
            if not 1 <= edit.expected_occurrences <= 1000:
                raise ValueError("expected_occurrences must be between 1 and 1000")
            count = content.count(edit.old_text)
            if count != edit.expected_occurrences:
                raise WorkspaceError(
                    f"{path}: expected {edit.expected_occurrences} occurrences, found {count}"
                )
            content = content.replace(edit.old_text, edit.new_text, edit.expected_occurrences)
            self.text_bytes(content, path=path)
            replacements += edit.expected_occurrences
        updated = _VirtualFile(self.text_bytes(content, path=path), existing.mode)
        self.set(path, updated)
        changed = updated.data != existing.data
        return MutationDiagnostic(
            0,
            "replace_text",
            path,
            "ready" if changed else "no_op",
            changed,
            before,
            updated.sha256,
            f"{replacements} exact replacement(s)",
            None,
        )

    def _write(self, operation: WriteMutation) -> MutationDiagnostic:
        path, existing = self.require_existing(operation.path)
        self.validate_sha(operation.expected_sha256, existing.sha256, path=path)
        data = self.text_bytes(operation.content, path=path)
        mode = existing.mode if operation.preserve_mode else 0o644
        updated = _VirtualFile(data, mode)
        self.set(path, updated)
        changed = updated != existing
        return MutationDiagnostic(
            0,
            "write",
            path,
            "ready" if changed else "no_op",
            changed,
            existing.sha256,
            updated.sha256,
            f"{len(data)} UTF-8 bytes",
            None,
        )

    def _create(self, operation: CreateMutation) -> MutationDiagnostic:
        path = self.require_absent(operation.path)
        if not 0 <= operation.mode <= 0o777:
            raise ValueError("create mode must be between 0 and 0o777")
        updated = _VirtualFile(self.text_bytes(operation.content, path=path), operation.mode)
        self.set(path, updated)
        return MutationDiagnostic(
            0,
            "create",
            path,
            "ready",
            True,
            None,
            updated.sha256,
            f"create {len(updated.data)} UTF-8 bytes",
            None,
        )

    def _delete(self, operation: DeleteMutation) -> MutationDiagnostic:
        path, existing = self.require_existing(operation.path)
        self.validate_sha(operation.expected_sha256, existing.sha256, path=path)
        self.set(path, None)
        return MutationDiagnostic(
            0,
            "delete",
            path,
            "ready",
            True,
            existing.sha256,
            None,
            "delete regular file",
            None,
        )

    def _move(self, operation: MoveMutation) -> MutationDiagnostic:
        source, existing = self.require_existing(operation.source)
        self.validate_sha(operation.expected_source_sha256, existing.sha256, path=source)
        destination = self.require_absent(operation.destination)
        if source == destination:
            raise WorkspaceError("Move source and destination must differ")
        self.set(source, None)
        self.set(destination, existing)
        self.move_hints.append((source, destination))
        return MutationDiagnostic(
            0,
            "move",
            f"{source} -> {destination}",
            "ready",
            True,
            existing.sha256,
            existing.sha256,
            "move without content change",
            None,
        )

    def _patch(self, operation: ApplyPatchMutation) -> MutationDiagnostic:
        def read_file(raw: str) -> str | None:
            path, value = self.load(raw)
            return None if value is None else self.text(value, path=path)

        normalized = normalize_patch(operation.patch, read_file)
        validate_patch(
            normalized.patch,
            self.repo,  # type: ignore[arg-type]
            max_chars=self.ctx.config.server.max_tool_output_chars * 4,
        )
        materialized = materialize_normalized_patch(normalized.patch, read_file)
        changed = False
        before_hashes: list[str] = []
        after_hashes: list[str] = []
        for raw, content in materialized.items():
            path, existing = self.load(raw)
            if existing is not None:
                before_hashes.append(existing.sha256)
            if content is None:
                self.set(path, None)
                changed = changed or existing is not None
                continue
            mode = existing.mode if existing is not None else 0o644
            updated = _VirtualFile(self.text_bytes(content, path=path), mode)
            self.set(path, updated)
            after_hashes.append(updated.sha256)
            changed = changed or updated != existing
        paths = tuple(sorted(materialized))
        return MutationDiagnostic(
            0,
            "apply_patch",
            ", ".join(paths),
            "ready" if changed else "no_op",
            changed,
            hashlib.sha256("".join(before_hashes).encode()).hexdigest() if before_hashes else None,
            hashlib.sha256("".join(after_hashes).encode()).hexdigest() if after_hashes else None,
            f"{normalized.input_format}; {len(paths)} path(s)",
            None,
            normalized.repair_actions,
        )

    def _restore(self, operation: RestoreMutation) -> MutationDiagnostic:
        if not operation.paths:
            raise ValueError("restore paths must contain at least one path")
        changed = False
        restored: list[str] = []
        for raw in operation.paths:
            path, existing = self.load(raw)
            try:
                blob = self.ctx.git.read_snapshot_blob(
                    self.workspace,
                    self.repo,  # type: ignore[arg-type]
                    self.head_sha,
                    path,
                )
            except RepoForgeError as exc:
                if exc.code is not ErrorCode.NOT_FOUND:
                    raise
                target = None
            else:
                target = _VirtualFile(blob.data, int(blob.mode[-3:], 8))
            self.set(path, target)
            changed = changed or target != existing
            restored.append(path)
        return MutationDiagnostic(
            0,
            "restore",
            ", ".join(sorted(restored)),
            "ready" if changed else "no_op",
            changed,
            None,
            None,
            f"restore {len(restored)} path(s) from HEAD",
            None,
        )

    def transaction_actions(self) -> tuple[TransactionAction, ...]:
        actions: list[TransactionAction] = []
        handled: set[str] = set()
        for source, destination in self.move_hints:
            original_source = self.original.get(source)
            original_destination = self.original.get(destination)
            current_source = self.current.get(source)
            current_destination = self.current.get(destination)
            if (
                original_source is not None
                and original_destination is None
                and current_source is None
                and current_destination == original_source
            ):
                actions.append(MoveFile(source, destination))
                handled.update((source, destination))
        for path in sorted(set(self.original) | set(self.current)):
            if path in handled:
                continue
            before = self.original.get(path)
            after = self.current.get(path)
            if before == after:
                continue
            if before is None and after is not None:
                actions.append(CreateFile(path, after.data, after.mode))
            elif before is not None and after is None:
                actions.append(DeleteFile(path))
            elif before is not None and after is not None:
                actions.append(WriteFile(path, after.data, preserve_mode=True))
        return tuple(actions)


class WorkspaceMutator:
    def __init__(self, ctx: ApplicationContext):
        self.ctx = ctx

    def execute(self, command: WorkspaceMutateCommand) -> WorkspaceMutateResult:
        operations = tuple(command.operations)
        if not operations or len(operations) > _MAX_OPERATIONS:
            raise ValueError(f"operations must contain between 1 and {_MAX_OPERATIONS} entries")
        if _SHA256.fullmatch(command.expected_workspace_fingerprint) is None:
            raise ValueError("expected_workspace_fingerprint must be a lowercase SHA-256")
        transaction_factory = self.ctx.file_transactions
        if transaction_factory is None:
            raise ConfigError("File transaction adapter is unavailable")
        record, repo, workspace = self.ctx.workspace(command.workspace_id)
        op_names = tuple(self._op_name(operation) for operation in operations)
        audit_details: dict[str, object] = {
            "workspace_id": command.workspace_id,
            "operation_count": len(operations),
            "ops": list(op_names),
            "dry_run": command.dry_run,
        }

        def run() -> WorkspaceMutateResult:
            with self.ctx.locks.lock(command.workspace_id):
                engine = transaction_factory.create(workspace)
                recovery = engine.recover_pending()
                audit_details["recovered_rolled_back"] = recovery.rolled_back
                audit_details["recovered_finalized"] = recovery.finalized
                before_lookup = read_fingerprint(
                    self.ctx.fingerprint_cache,
                    command.workspace_id,
                    self.ctx.git,
                    workspace,
                )
                if before_lookup.fingerprint != command.expected_workspace_fingerprint:
                    raise WorkspaceError(
                        "Workspace changed since it was inspected; refresh status before mutating"
                    )
                head_sha = self.ctx.git.head_sha(workspace)
                planner = _MutationPlanner(self.ctx, workspace, repo, head_sha)
                diagnostics: list[MutationDiagnostic] = []
                for index, operation in enumerate(operations):
                    op_name = op_names[index]
                    if op_name not in repo.allowed_mutation_ops:
                        error: Exception = SecurityError(
                            f"{op_name} mutation is disabled by repository policy"
                        )
                        if not command.dry_run:
                            raise error
                        diagnostics.append(
                            MutationDiagnostic(
                                index,
                                op_name,
                                self._display_path(operation),
                                "failed",
                                False,
                                None,
                                None,
                                None,
                                str(error),
                            )
                        )
                        continue
                    snapshot = dict(planner.current)
                    move_count = len(planner.move_hints)
                    try:
                        diagnostic = planner.apply(operation)
                    except Exception as exc:
                        planner.current = snapshot
                        del planner.move_hints[move_count:]
                        if not command.dry_run:
                            raise
                        diagnostics.append(
                            MutationDiagnostic(
                                index,
                                op_name,
                                self._display_path(operation),
                                "failed",
                                False,
                                None,
                                None,
                                None,
                                str(exc),
                            )
                        )
                    else:
                        diagnostics.append(
                            MutationDiagnostic(
                                index,
                                diagnostic.op,
                                diagnostic.path,
                                diagnostic.status,
                                diagnostic.changed,
                                diagnostic.before_sha256,
                                diagnostic.after_sha256,
                                diagnostic.candidate_context,
                                diagnostic.failure_reason,
                                diagnostic.repair_actions,
                            )
                        )
                actions = planner.transaction_actions()
                ready = all(item.status != "failed" for item in diagnostics)
                would_change = any(item.changed for item in diagnostics)
                if command.dry_run:
                    return WorkspaceMutateResult(
                        command.workspace_id,
                        True,
                        ready,
                        False,
                        would_change,
                        len(operations),
                        tuple(diagnostics),
                        (),
                        before_lookup.fingerprint,
                        head_sha,
                        self.ctx.git.diff_stat(workspace),
                        self.ctx.git.change_metrics(workspace, repo),
                        None,
                    )
                if not actions:
                    return WorkspaceMutateResult(
                        command.workspace_id,
                        False,
                        True,
                        False,
                        False,
                        len(operations),
                        tuple(diagnostics),
                        (),
                        before_lookup.fingerprint,
                        head_sha,
                        self.ctx.git.diff_stat(workspace),
                        self.ctx.git.change_metrics(workspace, repo),
                        None,
                    )

                def validate_budget() -> None:
                    self.ctx.git.enforce_change_budget(workspace, repo)

                receipt = engine.commit(
                    TransactionPlan(actions),
                    precommit_validator=validate_budget,
                )
                after = prime_fingerprint(
                    self.ctx.fingerprint_cache,
                    command.workspace_id,
                    self.ctx.git,
                    workspace,
                )
                record.last_verification = None
                self.ctx.store.save(record)
                metrics = self.ctx.git.change_metrics(workspace, repo)
                return WorkspaceMutateResult(
                    command.workspace_id,
                    False,
                    True,
                    True,
                    True,
                    len(operations),
                    tuple(diagnostics),
                    receipt.changed_paths,
                    after.fingerprint,
                    self.ctx.git.head_sha(workspace),
                    self.ctx.git.diff_stat(workspace),
                    metrics,
                    receipt.transaction_id,
                )

        return self.ctx.audited("workspace_mutate", audit_details, run)

    @staticmethod
    def _op_name(operation: WorkspaceMutation) -> str:
        if isinstance(operation, ReplaceTextMutation):
            return "replace_text"
        if isinstance(operation, WriteMutation):
            return "write"
        if isinstance(operation, CreateMutation):
            return "create"
        if isinstance(operation, DeleteMutation):
            return "delete"
        if isinstance(operation, MoveMutation):
            return "move"
        if isinstance(operation, ApplyPatchMutation):
            return "apply_patch"
        return "restore"

    @staticmethod
    def _display_path(operation: WorkspaceMutation) -> str | None:
        if isinstance(operation, MoveMutation):
            return f"{operation.source} -> {operation.destination}"
        if isinstance(operation, ApplyPatchMutation):
            return None
        if isinstance(operation, RestoreMutation):
            return ", ".join(operation.paths)
        return operation.path
