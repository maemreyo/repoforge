"""Boundaries for building, installing, and activating immutable runtime releases.

The upgrade pipeline is expressed against these ports so its orchestration -- clean
check, build, smoke, atomic swap, receipt, rollback -- is exercised with fakes and
never has to spawn `uv`, touch git, or restart a live supervisor to be tested.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ..domain.activation import ActivationReceipt, ReleaseManifest


@dataclass(frozen=True, slots=True)
class WorktreeState:
    """The exact source identity an upgrade is built from."""

    head_sha: str
    clean: bool
    dirty_detail: str = ""


@dataclass(frozen=True, slots=True)
class BuildArtifact:
    """A wheel built from a worktree, with the fingerprint that identifies it."""

    wheel_path: Path
    build_fingerprint: str
    package_version: str


@dataclass(frozen=True, slots=True)
class SmokeResult:
    """The verdict of running a candidate release against itself."""

    ok: bool
    tool_surface_hash: str
    detail: str


class WorktreeInspector(Protocol):
    """Report whether a worktree is clean and what commit it is at."""

    def inspect(self, worktree: Path) -> WorktreeState: ...


class ReleaseBuilder(Protocol):
    """Build exactly one wheel from a clean worktree."""

    def build(self, worktree: Path) -> BuildArtifact: ...


class ReleaseInstaller(Protocol):
    """Materialize an immutable, self-contained release environment from a wheel."""

    def install(self, wheel: Path, destination: Path) -> None: ...


class ReleaseSmokeTester(Protocol):
    """Run health/schema/tool-surface checks using the newly installed release itself."""

    def smoke(self, release_path: Path) -> SmokeResult: ...


class SupervisorReloader(Protocol):
    """Ask the running supervisor to adopt whatever ``current`` now points at."""

    def reload(self) -> bool: ...


@dataclass(frozen=True, slots=True)
class HealthSample:
    """One post-activation health observation of the running runtime."""

    healthy: bool
    detail: str


class RuntimeHealthProbe(Protocol):
    """Sample the live runtime's health during the post-activation window (#272)."""

    def sample(self) -> HealthSample: ...


class ReleaseStore(Protocol):
    """The immutable-release layout the application orchestrates over.

    Implemented by the filesystem adapter; the application and CLI depend only on
    this boundary so they never import the concrete store directly.
    """

    @property
    def root(self) -> Path: ...
    def release_path(self, commit_sha: str) -> Path: ...
    def bin_launcher(self) -> Path: ...
    def write_launcher_shim(self) -> Path: ...
    def write_manifest(self, manifest: ReleaseManifest) -> None: ...
    def read_manifest(self, commit_sha: str) -> ReleaseManifest | None: ...
    def installed_shas(self) -> list[str]: ...
    def list_releases(self) -> list[ReleaseManifest]: ...
    def current_sha(self) -> str | None: ...
    def previous_sha(self) -> str | None: ...
    def swap_current(self, commit_sha: str) -> str | None: ...
    def rollback(self) -> str: ...
    def prune(self, *, keep: int) -> list[str]: ...
    def write_receipt(self, receipt: ActivationReceipt) -> Path: ...
    def read_receipt(self, receipt_id: str) -> ActivationReceipt | None: ...
    def list_receipts(self) -> list[ActivationReceipt]: ...
    def allocate_receipt_id(self, *, date_stamp: str) -> str: ...
