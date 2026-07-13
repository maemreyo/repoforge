from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar

from ..config import AppConfig, RepositoryConfig
from ..domain.errors import ConfigError, SecurityError, WorkspaceError
from ..domain.policy import validate_branch
from ..domain.workspace import WorkspaceRecord
from ..ports import (
    AuditSink,
    Clock,
    CommandExecutor,
    ExecutableLocator,
    FileSystem,
    GitRepository,
    IdGenerator,
    LockManager,
    OperationGate,
    PullRequestGateway,
    WorkspaceStore,
)

T = TypeVar("T")
_MUTATING_ACTIONS = {
    "workspace_create",
    "workspace_write_file",
    "workspace_replace_text",
    "workspace_apply_patch",
    "workspace_restore_paths",
    "workspace_run_profile",
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
    "workspace_run_profile",
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

    def repo(self, repo_id: str) -> RepositoryConfig:
        try:
            repo = self.config.repositories[repo_id]
        except KeyError as exc:
            raise ConfigError(f"Unknown repository id: {repo_id}") from exc
        if not repo.path.is_dir() or not (repo.path / ".git").exists():
            raise ConfigError(f"Configured path is not a Git working tree: {repo.path}")
        return repo

    def workspace(self, workspace_id: str) -> tuple[WorkspaceRecord, RepositoryConfig, Path]:
        record = self.store.load(workspace_id)
        repo = self.repo(record.repo_id)
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

    def audited(
        self,
        action: str,
        details: dict[str, Any],
        operation: Callable[[], T],
        *,
        mutating: bool | None = None,
    ) -> T:
        correlation_id = self.ids.new_hex(24)
        is_mutating = action in _MUTATING_ACTIONS if mutating is None else mutating
        started = time.monotonic()
        try:
            with self.gate.operation(correlation_id, mutating=is_mutating):
                if action in _POLICY_WRITE_ACTIONS:
                    repo_id = details.get("repo_id")
                    if not isinstance(repo_id, str):
                        workspace_id = details.get("workspace_id")
                        if isinstance(workspace_id, str):
                            repo_id = self.store.load(workspace_id).repo_id
                    if isinstance(repo_id, str):
                        repo = self.repo(repo_id)
                        if repo.read_only:
                            raise SecurityError(
                                f"Repository {repo_id!r} is enrolled read-only; choose and approve "
                                "a writable policy before mutation"
                            )
                        if action in _PUBLISH_ACTIONS and not repo.publish_enabled:
                            raise SecurityError(
                                f"Repository {repo_id!r} is enrolled local-only; configure and approve publishing access first"
                            )
                result = operation()
        except Exception as exc:
            self.audit.record(
                action,
                success=False,
                details={
                    **details,
                    "correlation_id": correlation_id,
                    "duration_ms": round((time.monotonic() - started) * 1000, 3),
                    "error_type": type(exc).__name__,
                    "error_code": str(
                        getattr(
                            getattr(exc, "code", None),
                            "value",
                            getattr(exc, "code", "INTERNAL_ERROR"),
                        )
                    ),
                    "retryable": bool(getattr(exc, "retryable", False)),
                },
            )
            raise
        self.audit.record(
            action,
            success=True,
            details={
                **details,
                "correlation_id": correlation_id,
                "duration_ms": round((time.monotonic() - started) * 1000, 3),
            },
        )
        return result
