from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, TypeVar
from ..config import AppConfig, RepositoryConfig
from ..domain.errors import ConfigError, WorkspaceError
from ..domain.policy import validate_branch
from ..domain.workspace import WorkspaceRecord
from ..ports import (
    AuditSink,
    Clock,
    ExecutableLocator,
    FileSystem,
    GitRepository,
    CommandExecutor,
    IdGenerator,
    PullRequestGateway,
    WorkspaceStore,
)

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class ApplicationContext:
    config: AppConfig
    commands: CommandExecutor
    git: GitRepository
    github: PullRequestGateway
    filesystem: FileSystem
    store: WorkspaceStore
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

    def workspace(
        self, workspace_id: str
    ) -> tuple[WorkspaceRecord, RepositoryConfig, Path]:
        record = self.store.load(workspace_id)
        repo = self.repo(record.repo_id)
        path = Path(record.path).resolve()
        root = self.config.server.workspace_root.resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise WorkspaceError(
                f"Workspace path is outside workspace_root: {path}"
            ) from exc
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
        self, action: str, details: dict[str, Any], operation: Callable[[], T]
    ) -> T:
        try:
            result = operation()
        except Exception as exc:
            self.audit.record(
                action,
                success=False,
                details={**details, "error_type": type(exc).__name__},
            )
            raise
        self.audit.record(action, success=True, details=details)
        return result
