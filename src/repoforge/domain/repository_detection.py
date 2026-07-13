"""Pure repository facts gathered by a read-only probe."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class RemoteFact:
    name: str
    fetch_url: str | None
    push_url: str | None


@dataclass(frozen=True, slots=True)
class ManifestFact:
    path: str
    ecosystem: str
    package_manager: str | None
    workspace_root: bool = False
    scripts: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RepositoryFacts:
    root: Path
    common_dir: Path
    repo_id: str
    display_name: str
    current_branch: str | None
    default_branch_candidates: tuple[str, ...]
    remotes: tuple[RemoteFact, ...]
    manifests: tuple[ManifestFact, ...]
    lockfiles: tuple[str, ...]
    toolchain_declarations: tuple[str, ...]
    scripts: tuple[str, ...]
    make_targets: tuple[str, ...]
    instruction_files: tuple[str, ...]
    ci_files: tuple[str, ...]
    workspace_packages: tuple[str, ...]
    submodules: tuple[str, ...]
    lfs_tracked: bool
    shallow: bool
    detached: bool
    symlink_count: int
    large_file_count: int
    binary_file_count: int
    tracked_file_count: int
    total_tracked_bytes: int
    existing_worktrees: tuple[str, ...]
    policy_files: tuple[str, ...] = ()
    github_authenticated: bool | None = None
    scan_truncated: bool = False
    warnings: tuple[str, ...] = ()
