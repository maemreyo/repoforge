from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, TypeVar

from ..config import AppConfig, RepositoryConfig
from ..domain.errors import ConfigError, ErrorCode, RepoForgeError, SecurityError, WorkspaceError
from ..domain.operations import automatic_retry_allowed, unchanged_state_for
from ..domain.policy import validate_branch
from ..domain.workspace import WorkspaceRecord
from ..ports import (
    AuditSink,
    Clock,
    CommandExecutor,
    ExecutableLocator,
    FileSystem,
    GitRepository,
    IdempotencyStore,
    IdGenerator,
    LockManager,
    MetricsSink,
    OperationGate,
    OperationStore,
    PullRequestGateway,
    WorkspaceStore,
)
from .idempotency import execute_idempotent

T = TypeVar("T")


_POLICY_SNAPSHOT_FIELDS = (
    "branch_prefix",
    "protected_branches",
    "allowed_paths",
    "denied_paths",
    "max_changed_files",
    "max_diff_lines",
    "max_total_changed_bytes",
)


def _policy_snapshot_digest(payload: dict[str, object]) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def repository_policy_snapshot(repo: RepositoryConfig) -> dict[str, object]:
    """Persist an integrity-protected read boundary for an orphaned workspace.

    Executable profiles and publishing capability are deliberately excluded.
    """
    payload: dict[str, object] = {
        "branch_prefix": repo.branch_prefix,
        "protected_branches": list(repo.protected_branches),
        "allowed_paths": list(repo.allowed_paths),
        "denied_paths": list(repo.denied_paths),
        "max_changed_files": repo.max_changed_files,
        "max_diff_lines": repo.max_diff_lines,
        "max_total_changed_bytes": repo.max_total_changed_bytes,
    }
    return {**payload, "sha256": _policy_snapshot_digest(payload)}


def _verified_policy_snapshot(raw: object) -> dict[str, object]:
    if not isinstance(raw, dict):
        return {}
    expected_keys = {*_POLICY_SNAPSHOT_FIELDS, "sha256"}
    if set(raw) != expected_keys:
        return {}
    digest = raw.get("sha256")
    if not isinstance(digest, str) or len(digest) != 64:
        return {}
    payload = {field: raw[field] for field in _POLICY_SNAPSHOT_FIELDS}
    if not hmac.compare_digest(digest, _policy_snapshot_digest(payload)):
        return {}
    return payload


def _snapshot_strings(raw: object, key: str, default: tuple[str, ...]) -> tuple[str, ...]:
    if not isinstance(raw, dict):
        return default
    value = raw.get(key)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return default
    return tuple(value)


def orphaned_repository_config(record: WorkspaceRecord) -> RepositoryConfig:
    """Return a fail-closed read-only policy for a workspace whose repository was removed."""
    raw = record.metadata.get("repository_policy_snapshot")
    snapshot = _verified_policy_snapshot(raw)
    branch_prefix = snapshot.get("branch_prefix")
    if not isinstance(branch_prefix, str) or not branch_prefix:
        branch_prefix = record.branch.rsplit("/", 1)[0] + "/" if "/" in record.branch else "ai/"
    allowed_paths = _snapshot_strings(
        snapshot,
        "allowed_paths",
        ("__repoforge_orphaned_metadata_only__",),
    )
    denied_paths = _snapshot_strings(snapshot, "denied_paths", ("**",))

    def positive(name: str, default: int) -> int:
        value = snapshot.get(name)
        return (
            value
            if isinstance(value, int) and not isinstance(value, bool) and value > 0
            else default
        )

    return RepositoryConfig(
        repo_id=record.repo_id,
        path=Path(record.path).resolve(),
        display_name=f"{record.repo_id} (orphaned read-only)",
        remote=record.remote,
        default_base=record.base,
        allowed_base_branches=(record.base,),
        branch_prefix=branch_prefix,
        protected_branches=_snapshot_strings(
            snapshot, "protected_branches", ("main", "master", "develop", "production")
        ),
        read_only=True,
        publish_enabled=False,
        require_verification_before_commit=True,
        fetch_before_workspace=False,
        max_changed_files=positive("max_changed_files", 1),
        max_diff_lines=positive("max_diff_lines", 1),
        max_total_changed_bytes=positive("max_total_changed_bytes", 1),
        allowed_paths=allowed_paths,
        denied_paths=denied_paths,
        profiles={},
    )


_MUTATING_ACTIONS = {
    "workspace_create",
    "workspace_write_file",
    "workspace_replace_text",
    "workspace_apply_patch",
    "workspace_restore_paths",
    "workspace_refresh",
    "workspace_run_profile",
    "workspace_run_diagnostic",
    "workspace_verify",
    "workspace_commit",
    "workspace_push",
    "workspace_create_draft_pr",
    "workspace_update_draft_pr",
    "workspace_remove",
}

_PUBLISH_ACTIONS = {
    "workspace_push",
    "workspace_create_draft_pr",
    "workspace_update_draft_pr",
}

_POLICY_WRITE_ACTIONS = {
    "workspace_write_file",
    "workspace_replace_text",
    "workspace_apply_patch",
    "workspace_restore_paths",
    "workspace_refresh",
    "workspace_run_profile",
    "workspace_run_diagnostic",
    "workspace_verify",
    "workspace_commit",
    "workspace_push",
    "workspace_create_draft_pr",
    "workspace_update_draft_pr",
}


@dataclass(frozen=True, slots=True)
class ApplicationContext:
    config: AppConfig
    commands: CommandExecutor
    git: GitRepository
    github: PullRequestGateway
    filesystem: FileSystem
    store: WorkspaceStore
    locks: LockManager
    gate: OperationGate
    audit: AuditSink
    clock: Clock
    ids: IdGenerator
    executables: ExecutableLocator
    metrics: MetricsSink | None = None
    idempotency: IdempotencyStore | None = None
    operation_store: OperationStore | None = None

    def now_epoch(self) -> float:
        try:
            return datetime.fromisoformat(self.clock.now_iso()).timestamp()
        except ValueError as exc:
            raise ConfigError("Clock returned a non-ISO timestamp") from exc

    def repo(self, repo_id: str) -> RepositoryConfig:
        try:
            repo = self.config.repositories[repo_id]
        except KeyError as exc:
            raise ConfigError(f"Unknown repository id: {repo_id}") from exc
        if not repo.path.is_dir() or not (repo.path / ".git").exists():
            raise ConfigError(f"Configured path is not a Git working tree: {repo.path}")
        return repo

    def repository_for_workspace(self, record: WorkspaceRecord) -> RepositoryConfig:
        repo = self.config.repositories.get(record.repo_id)
        if repo is not None:
            if not repo.path.is_dir() or not (repo.path / ".git").exists():
                raise ConfigError(f"Configured path is not a Git working tree: {repo.path}")
            return repo
        return orphaned_repository_config(record)

    def workspace(self, workspace_id: str) -> tuple[WorkspaceRecord, RepositoryConfig, Path]:
        record = self.store.load(workspace_id)
        repo = self.repository_for_workspace(record)
        path = Path(record.path).resolve()
        root = self.config.server.workspace_root.resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise WorkspaceError(f"Workspace path is outside workspace_root: {path}") from exc
        if not path.is_dir() or not (path / ".git").exists():
            raise WorkspaceError(f"Workspace is missing or invalid: {path}")
        branch = self.git.current_branch(path)
        if branch != record.branch:
            raise WorkspaceError(
                f"Workspace branch changed unexpectedly: registry={record.branch}, actual={branch}"
            )
        validate_branch(branch, repo)
        return (record, repo, path)

    def record_metric(
        self, action: str, *, success: bool, duration_ms: float, error_code: str | None
    ) -> None:
        if self.metrics is None:
            return
        with contextlib.suppress(Exception):
            self.metrics.record(
                action,
                success=success,
                duration_ms=duration_ms,
                error_code=error_code,
            )

    def audited(
        self,
        action: str,
        details: dict[str, Any],
        operation: Callable[[], T],
        *,
        mutating: bool | None = None,
        correlation_id: str | None = None,
    ) -> T:
        correlation = correlation_id or self.ids.new_hex(24)
        is_mutating = action in _MUTATING_ACTIONS if mutating is None else mutating
        started = time.monotonic()
        try:
            with self.gate.operation(correlation, mutating=is_mutating):
                if action in _POLICY_WRITE_ACTIONS:
                    repo_id = details.get("repo_id")
                    if not isinstance(repo_id, str):
                        workspace_id = details.get("workspace_id")
                        if isinstance(workspace_id, str):
                            repo_id = self.store.load(workspace_id).repo_id
                    if isinstance(repo_id, str):
                        workspace_id = details.get("workspace_id")
                        if repo_id in self.config.repositories:
                            repo = self.repo(repo_id)
                            orphaned = False
                        elif isinstance(workspace_id, str):
                            record = self.store.load(workspace_id)
                            repo = self.repository_for_workspace(record)
                            orphaned = True
                        else:
                            repo = self.repo(repo_id)
                            orphaned = False
                        if repo.read_only:
                            reason = (
                                "orphaned_read_only: the repository was removed from the active "
                                "generation; only policy-bounded reads remain available"
                                if orphaned
                                else "choose and approve a writable policy before mutation"
                            )
                            raise SecurityError(
                                f"Repository {repo_id!r} is enrolled read-only; {reason}"
                            )
                        if action in _PUBLISH_ACTIONS and not repo.publish_enabled:
                            raise SecurityError(
                                f"Repository {repo_id!r} is enrolled local-only; configure and approve publishing access first"
                            )
                result = operation()
        except Exception as exc:
            duration = round((time.monotonic() - started) * 1000, 3)
            code = str(
                getattr(
                    getattr(exc, "code", None),
                    "value",
                    getattr(exc, "code", "INTERNAL_ERROR"),
                )
            )
            if isinstance(exc, RepoForgeError):
                if exc.correlation_id is None:
                    exc.correlation_id = correlation
                if is_mutating and not exc.unchanged_state:
                    exc.unchanged_state = unchanged_state_for(action)
            try:
                normalized_code = ErrorCode(code)
            except ValueError:
                normalized_code = ErrorCode.INTERNAL_ERROR
            try:
                self.audit.record(
                    action,
                    success=False,
                    details={
                        **details,
                        "correlation_id": correlation,
                        "duration_ms": duration,
                        "error_type": type(exc).__name__,
                        "error_code": code,
                        "retryable": bool(getattr(exc, "retryable", False)),
                        "automatic_retry_allowed": automatic_retry_allowed(
                            action,
                            normalized_code,
                            has_idempotency_key="idempotency_key_hash" in details,
                        ),
                    },
                )
            except Exception as audit_exc:
                add_note = getattr(exc, "add_note", None)
                if callable(add_note):
                    add_note(f"Audit append also failed: {type(audit_exc).__name__}")
            self.record_metric(action, success=False, duration_ms=duration, error_code=code)
            raise
        duration = round((time.monotonic() - started) * 1000, 3)
        self.audit.record(
            action,
            success=True,
            details={
                **details,
                "correlation_id": correlation,
                "duration_ms": duration,
            },
        )
        self.record_metric(action, success=True, duration_ms=duration, error_code=None)
        return result

    def idempotent(
        self,
        action: str,
        key: str | None,
        request: Any,
        operation: Callable[[], T],
        *,
        details: dict[str, Any] | None = None,
        serialize: Callable[[T], Any] | None = None,
        deserialize: Callable[[Any], T] | None = None,
    ) -> T:
        return execute_idempotent(
            self,
            action,
            key,
            request,
            operation,
            details=details,
            serialize=serialize,
            deserialize=deserialize,
        )
