"""Semantic Git repository/worktree boundary."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from ..config import ProfileConfig, RepositoryConfig
from .command import CommandExecutor, CommandResult


@dataclass(frozen=True, slots=True)
class ResolvedRepositoryRef:
    resolved_ref: str
    commit_sha: str


@dataclass(frozen=True, slots=True)
class GitSnapshotBlob:
    path: str
    object_sha: str
    mode: str
    size_bytes: int
    data: bytes


@dataclass(frozen=True, slots=True)
class GitActorIdentity:
    name: str
    email: str
    date: str


@dataclass(frozen=True, slots=True)
class GitChangedFileEvidence:
    status: str
    path: str
    previous_path: str | None
    additions: int | None
    deletions: int | None
    binary: bool


@dataclass(frozen=True, slots=True)
class GitCommitEvidence:
    tree_sha: str
    parent_shas: tuple[str, ...]
    comparison_parent_sha: str | None
    author: GitActorIdentity
    committer: GitActorIdentity
    subject: str
    body: str
    message_truncated: bool
    files: tuple[GitChangedFileEvidence, ...]
    total_files: int
    files_truncated: bool
    additions: int
    deletions: int
    binary_files: int
    omitted_paths: int
    patch: str | None
    patch_truncated: bool
    binary_patch_omitted: bool


@dataclass(frozen=True, slots=True)
class GitComparisonEvidence:
    merge_base_sha: str
    ahead: int
    behind: int
    files: tuple[GitChangedFileEvidence, ...]
    total_files: int
    files_truncated: bool
    additions: int
    deletions: int
    binary_files: int
    omitted_paths: int
    patch: str | None
    patch_truncated: bool
    binary_patch_omitted: bool


class GitRepository(Protocol):
    @property
    def executor(self) -> CommandExecutor: ...

    def is_worktree(self, path: Path) -> bool: ...

    def diff_stat(self, path: Path) -> str: ...

    def current_branch(self, path: Path) -> str: ...

    def head_sha(self, path: Path) -> str: ...

    def status_porcelain(self, path: Path) -> str: ...

    def status_short_branch(self, path: Path) -> str: ...

    def remote_verbose(self, path: Path) -> str: ...

    def changed_paths(self, path: Path, repo: RepositoryConfig) -> list[str]: ...

    def untracked_paths(self, path: Path, repo: RepositoryConfig) -> list[str]: ...

    def is_tracked_path(self, path: Path, relative_path: str) -> bool: ...

    def fingerprint(self, path: Path) -> str: ...

    def change_metrics(self, path: Path, repo: RepositoryConfig) -> dict[str, Any]: ...

    def enforce_change_budget(self, path: Path, repo: RepositoryConfig) -> dict[str, Any]: ...

    def ensure_clean(self, path: Path, *, context: str) -> None: ...

    def ahead_of_base(self, path: Path, remote: str, base: str) -> int: ...

    def list_files(
        self, path: Path, repo: RepositoryConfig, max_entries: int
    ) -> tuple[list[str], bool]: ...

    def root_files(self, path: Path, repo: RepositoryConfig) -> list[str]: ...

    def recent_commits(self, path: Path, limit: int) -> list[dict[str, str]]: ...

    def resolve_snapshot_ref(
        self, path: Path, repo: RepositoryConfig, ref: str | None
    ) -> ResolvedRepositoryRef: ...

    def list_snapshot_files(
        self,
        path: Path,
        repo: RepositoryConfig,
        commit_sha: str,
        max_entries: int,
    ) -> tuple[list[str], bool]: ...

    def read_snapshot_blob(
        self,
        path: Path,
        repo: RepositoryConfig,
        commit_sha: str,
        relative_path: str,
    ) -> GitSnapshotBlob: ...

    def search_snapshot(
        self,
        path: Path,
        repo: RepositoryConfig,
        commit_sha: str,
        query: str,
        path_glob: str | None,
        max_results: int,
    ) -> tuple[list[str], bool]: ...

    def read_commit_evidence(
        self,
        path: Path,
        repo: RepositoryConfig,
        snapshot: ResolvedRepositoryRef,
        max_files: int,
        include_patch: bool,
    ) -> GitCommitEvidence: ...

    def compare_commits(
        self,
        path: Path,
        repo: RepositoryConfig,
        base: ResolvedRepositoryRef,
        head: ResolvedRepositoryRef,
        path_glob: str | None,
        max_files: int,
        include_patch: bool,
    ) -> GitComparisonEvidence: ...

    def search(
        self,
        path: Path,
        repo: RepositoryConfig,
        query: str,
        path_glob: str | None,
        max_results: int,
    ) -> tuple[list[str], bool]: ...

    def diff(self, path: Path, repo: RepositoryConfig, *, staged: bool) -> dict[str, Any]: ...

    def run_profile(
        self, path: Path, profile: ProfileConfig
    ) -> tuple[list[CommandResult], str, dict[str, Any]]: ...

    def restore_paths(
        self, path: Path, repo: RepositoryConfig, relative_paths: list[str]
    ) -> tuple[list[str], list[str]]: ...

    def create_worktree(
        self, repo: RepositoryConfig, destination: Path, branch: str, base: str
    ) -> str: ...

    def remove_worktree(
        self, repo: RepositoryConfig, path: Path, branch: str, delete_branch: bool
    ) -> bool: ...

    def commit(self, path: Path, message: str) -> tuple[str, str]: ...

    def push(self, path: Path, remote: str, branch: str, timeout: int) -> CommandResult: ...

    def upstream_name(self, path: Path) -> str | None: ...

    def upstream_sha(self, path: Path) -> str: ...

    def apply_patch(self, path: Path, patch: str) -> None: ...

    def reverse_patch(self, path: Path, patch: str) -> None: ...

    def remote_url(self, path: Path, remote: str) -> CommandResult: ...

    def verify_base(self, path: Path, remote: str, base: str) -> CommandResult: ...
