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


class AgentSecretStatusView(Protocol):
    """Whether a durable agent secret exists, is safely stored, and is usable."""

    @property
    def usable(self) -> bool: ...
    @property
    def exists(self) -> bool: ...
    @property
    def regular_file(self) -> bool: ...
    @property
    def owner_matches(self) -> bool: ...
    @property
    def mode_secure(self) -> bool: ...
    @property
    def key_present(self) -> bool: ...
    @property
    def value_nonempty(self) -> bool: ...
    @property
    def parse_valid(self) -> bool: ...
    @property
    def keys(self) -> tuple[str, ...]: ...
    def problems(self) -> tuple[str, ...]: ...


class ReceiptHistoryView(Protocol):
    """Readable receipts, unreadable ids, and whether history is incomplete."""

    @property
    def valid(self) -> tuple[ActivationReceipt, ...]: ...
    @property
    def unreadable(self) -> tuple[str, ...]: ...
    @property
    def degraded(self) -> bool: ...
    @property
    def latest(self) -> ActivationReceipt | None: ...


@dataclass(frozen=True, slots=True)
class WorktreeState:
    """The exact source identity an upgrade is built from."""

    head_sha: str
    clean: bool
    dirty_detail: str = ""
    # Provenance for humans, not identity: empty on a detached HEAD, which is a normal
    # state for a release build and must never fail the upgrade.
    branch: str = ""
    subject: str = ""


@dataclass(frozen=True, slots=True)
class BuildArtifact:
    """A wheel built from an immutable commit snapshot, with its identity digests.

    ``build_fingerprint`` is the SHA-256 of the wheel bytes. ``source_digest`` is the
    Git tree object id of the exact commit the wheel was built from -- the immutable
    content digest of the source tree, so the release's commit SHA provably names the
    bytes inside the wheel instead of merely the directory the build was run from.
    """

    wheel_path: Path
    build_fingerprint: str
    package_version: str
    source_digest: str = ""


@dataclass(frozen=True, slots=True)
class SmokeResult:
    """The verdict of running a candidate release against itself.

    ``contract_identity`` is the canonical digest of the release's verified contract
    identity (packaged == computed). It is empty when the smoke tester did not probe
    (legacy releases and test fakes); a non-empty value that disagrees with the
    recorded manifest is a ``RELEASE_CONTRACT_IDENTITY_MISMATCH``.
    """

    ok: bool
    tool_surface_hash: str
    detail: str
    contract_identity: str = ""
    contract_mismatched_fields: tuple[str, ...] = ()
    contract_artifact_paths: tuple[str, ...] = ()
    contract_packaged_identity: str = ""
    contract_computed_identity: str = ""


class WorktreeInspector(Protocol):
    """Report whether a worktree is clean and what commit it is at."""

    def inspect(self, worktree: Path) -> WorktreeState: ...


class ReleaseBuilder(Protocol):
    """Build exactly one wheel from the immutable snapshot of ``commit_sha``.

    The worktree only SELECTS the commit; the wheel must be built from a detached
    materialization of that commit's tree, never from the mutable working directory.
    Otherwise a concurrent edit between the clean check and ``uv build`` ships a wheel
    carrying the commit's sha but not the commit's bytes.
    """

    def build(self, worktree: Path, *, commit_sha: str) -> BuildArtifact: ...


class ReleaseInstaller(Protocol):
    """Materialize an immutable, self-contained release environment from a wheel."""

    def install(self, wheel: Path, destination: Path) -> None: ...


class ReleaseSmokeTester(Protocol):
    """Run health/schema/tool-surface checks using the newly installed release itself."""

    def smoke(self, release_path: Path) -> SmokeResult: ...


@dataclass(frozen=True, slots=True)
class RestartOutcome:
    """Result of replacing the live runtime process so it re-execs through ``current``.

    ``reclamation`` carries the execution-worker reclamation evidence (#368) when the
    restarter reconciled orphaned workers, so the activation receipt can record it.
    """

    ok: bool
    detail: str
    pid: int | None = None
    reclamation: dict[str, object] | None = None


class RuntimeRestarter(Protocol):
    """Replace the live runtime process so it adopts whatever ``current`` points at.

    A new *release* is new code, so it cannot be adopted by an in-process config
    reload -- the process must be replaced. Implementations stop the running
    supervisor and start it again through the stable launcher, reclaiming execution
    workers of the departing release first (#368).

    ``preflight_reclaim`` is the read-only handoff check (#424): it must answer
    whether the reclamation can proceed (registry evidence complete, no possibly
    alive unproven worker, no survived kill) WITHOUT stopping anything, so a caller
    can refuse a switch/rollback while the healthy runtime keeps serving.
    """

    def preflight_reclaim(
        self, departing_release: str | None
    ) -> tuple[bool, str, dict[str, object] | None]:
        """Read-only handoff preflight: ``(ok, detail, evidence)`` with no side effects."""
        ...

    def restart(
        self,
        *,
        departing_release: str | None = None,
        target_release: str | None = None,
    ) -> RestartOutcome:
        """Replace the live runtime so it adopts whatever ``current`` points at.

        ``target_release`` is the release the replacement will serve; the restarter
        binds the replacement-scoped admission permit (F-012) to it so the permit
        for one release can never admit a spawn for another. ``None`` means the
        permit stays epoch-bound only (a same-release restart).
        """
        ...


@dataclass(frozen=True, slots=True)
class ObservedRuntime:
    """What the live runtime process actually is, as published by that process."""

    running_release_sha: str | None
    phase: str
    pid: int | None = None
    executable: str | None = None
    tool_surface_hash: str | None = None
    # The typed terminal error a fail-closed/failed runtime published (#367), so the
    # repair path can verify a release failed for a deterministic reason before
    # mutating `current`.
    last_error_code: str | None = None
    fail_closed_since: str | None = None


class ReleaseObserver(Protocol):
    """Report the release the live runtime is actually serving (not the desired one)."""

    def observe(self) -> ObservedRuntime: ...


@dataclass(frozen=True, slots=True)
class ReleaseProcess:
    """A live process executing from a release directory.

    ``release_installed`` is False once its tree has been removed underneath it: the
    process is then running code that no longer exists on disk, which is unrecoverable
    rather than merely stale. Reported as a fact, never inferred from the pointers.
    """

    pid: int
    ppid: int
    commit_sha: str
    executable: str
    release_installed: bool
    # The parent chain up to init, from the same table snapshot. Required because a
    # supervised process is often not a DIRECT child of the supervisor: the MCP worker's
    # parent is `tunnel-client`, which the supervisor owns.
    ancestor_pids: tuple[int, ...] = ()

    def supervised_by(self, supervisor_pid: int | None) -> bool:
        """Is this process the live supervisor, or descended from it?

        Deliberately NOT ``ppid == 1``. Under launchd -- which IS pid 1 on macOS -- the
        healthy production supervisor has ppid 1, so treating that as orphanhood reported
        the live runtime as abandoned and told the operator to kill it. Caught by running
        this against a real installation.
        """
        if supervisor_pid is None:
            return False
        return self.pid == supervisor_pid or supervisor_pid in self.ancestor_pids

    def as_dict(self) -> dict[str, object]:
        return {
            "pid": self.pid,
            "ppid": self.ppid,
            "commit_sha": self.commit_sha,
            "executable": self.executable,
            "release_installed": self.release_installed,
            "ancestor_pids": list(self.ancestor_pids),
        }


class ReleaseProcessInspector(Protocol):
    """Enumerate live processes running out of the release root.

    Raises rather than returning an empty tuple when the process table cannot be read:
    callers treat "nothing is running" as permission to delete a release tree.
    """

    def list_processes(self) -> tuple[ReleaseProcess, ...]: ...


@dataclass(frozen=True, slots=True)
class HealthSample:
    """One post-activation health observation of the running runtime."""

    healthy: bool
    detail: str


class RuntimeHealthProbe(Protocol):
    """Sample the live runtime's health during the post-activation window (#272)."""

    def sample(self) -> HealthSample: ...


class DevConfigProvisioner(Protocol):
    """Synthesize an isolated dev-runtime config from the production config (#271)."""

    def provision(
        self,
        base_config: Path,
        dev_config: Path,
        *,
        state_root: Path,
        tunnel_id: str,
        profile: str,
    ) -> None: ...


class ReleaseStore(Protocol):
    """The immutable-release layout the application orchestrates over.

    Implemented by the filesystem adapter; the application and CLI depend only on
    this boundary so they never import the concrete store directly.
    """

    @property
    def root(self) -> Path: ...
    def release_path(self, commit_sha: str) -> Path: ...
    def bin_launcher(self) -> Path: ...
    def path_launcher(self) -> Path | None: ...
    def supervisor_launcher(self) -> Path: ...
    def supervisor_agent_label(self) -> str: ...
    def agent_env_path(self) -> Path: ...
    def write_agent_env(self, values: dict[str, str]) -> Path: ...
    def agent_secret_status(self) -> AgentSecretStatusView: ...
    def write_supervisor_shim(self, *, force: bool = False) -> Path: ...
    def write_internal_launcher_shim(self, *, force: bool = False) -> Path: ...
    def install_path_launcher(self, *, force: bool = False) -> Path | None: ...
    def reserve_release(self, commit_sha: str, *, build_fingerprint: str) -> bool: ...
    def write_manifest(self, manifest: ReleaseManifest) -> None: ...
    def read_manifest(self, commit_sha: str) -> ReleaseManifest | None: ...
    def installed_shas(self) -> list[str]: ...
    def list_releases(self) -> list[ReleaseManifest]: ...
    def current_sha(self) -> str | None: ...
    def previous_sha(self) -> str | None: ...
    def swap_current(self, commit_sha: str) -> str | None: ...
    def rollback(self) -> str: ...
    def retention_candidates(self, *, keep: int) -> list[str]: ...
    def prune(self, *, keep: int, protect: frozenset[str] = frozenset()) -> list[str]: ...
    def journal_path(self) -> Path: ...
    def begin_activation(
        self,
        *,
        receipt_id: str,
        from_sha: str | None,
        to_sha: str,
        transition_id: str | None = None,
    ) -> None: ...
    def record_activation_stage(self, stage: str) -> None: ...
    def read_in_flight_activation(self) -> dict[str, object] | None: ...
    def end_activation(self) -> None: ...
    def write_receipt(self, receipt: ActivationReceipt) -> Path: ...
    def read_receipt(self, receipt_id: str) -> ActivationReceipt | None: ...
    def list_receipts(self) -> list[ActivationReceipt]: ...
    def receipt_history(self) -> ReceiptHistoryView: ...
    def allocate_receipt_id(self, *, date_stamp: str) -> str: ...
    def write_worker_reclamation_artifact(self, reclamation: dict[str, object]) -> tuple[str, str]:
        """Persist full reclamation evidence immutably; return (artifact_id, sha256 digest).

        The receipt carries only a bounded summary referencing this artifact (#424).
        """
        ...

    def read_worker_reclamation_artifact(self, artifact_id: str) -> dict[str, object] | None: ...


class SupervisorKickstarter(Protocol):
    """Restart the supervisor through the OS process manager, keeping its ownership.

    When the supervisor is registered with launchd, restarting it by spawning our own
    detached process would move it *out* of launchd's control (a clean SHUTDOWN exit is
    not relaunched under ``SuccessfulExit: False``). Kickstarting the registered job
    instead keeps the OS as the owner across upgrades.
    """

    def available(self) -> bool: ...
    def kickstart(self) -> RestartOutcome: ...
