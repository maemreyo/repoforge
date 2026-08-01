"""Concrete build/install/smoke/inspect adapters for the upgrade pipeline.

These are thin shells over ``git`` and ``uv`` that run in the launcher/CLI context.
The orchestration that uses them (``UpgradeService``) is unit-tested with fakes; these
adapters carry only the subprocess wiring.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ...application.runtime.execution_worker_reconciler import ExecutionWorkerReconciler
from ...contracts.registry import contract_identity_digest
from ...domain.errors import ConfigError
from ...domain.runtime import ControlCommand, ControlRequest, RuntimePhase, RuntimeRecord
from ...ports.activation import (
    BuildArtifact,
    HealthSample,
    ObservedRuntime,
    RestartOutcome,
    SmokeResult,
    SupervisorKickstarter,
    WorktreeState,
)
from ...ports.runtime_control import RuntimeControlClient, RuntimeLauncher, RuntimeStore
from ...ports.sleeper import Sleeper

_VENV = "venv"


@dataclass(frozen=True, slots=True)
class _ContractProbe:
    """Structured packaged-vs-computed contract identity from the candidate release."""

    packaged: dict[str, object]
    computed: dict[str, object]
    mismatched_fields: tuple[str, ...]
    artifact_paths: tuple[str, ...]
    agreement: bool


def _run(
    argv: list[str], *, cwd: Path | None = None, timeout: float = 600.0
) -> tuple[int, str, str]:
    try:
        completed = subprocess.run(
            argv,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ConfigError(f"COMMAND_FAILED: {' '.join(argv)}: {exc}") from exc
    return completed.returncode, completed.stdout, completed.stderr


class GitWorktreeInspector:
    """Report the worktree's HEAD commit and whether it is clean."""

    def inspect(self, worktree: Path) -> WorktreeState:
        code, out, err = _run(["git", "-C", str(worktree), "rev-parse", "HEAD"], timeout=30.0)
        if code != 0:
            raise ConfigError(f"GIT_HEAD_UNKNOWN: {err.strip() or out.strip()}")
        head = out.strip().lower()
        code, out, err = _run(["git", "-C", str(worktree), "status", "--porcelain"], timeout=30.0)
        if code != 0:
            raise ConfigError(f"GIT_STATUS_FAILED: {err.strip() or out.strip()}")
        dirty = out.strip()
        return WorktreeState(
            head_sha=head,
            clean=not dirty,
            dirty_detail=_first_lines(dirty, 5),
            branch=self._branch(worktree),
            subject=self._subject(worktree),
        )

    @staticmethod
    def _branch(worktree: Path) -> str:
        """The checked-out branch, or "" on a detached HEAD.

        Best-effort by design: this is a label for humans reading a release list, so a
        detached HEAD or an unexpected git failure must degrade to "unknown" rather than
        refuse to build a release that is otherwise perfectly identified by its sha.
        """
        code, out, _ = _run(
            ["git", "-C", str(worktree), "symbolic-ref", "--quiet", "--short", "HEAD"],
            timeout=30.0,
        )
        return out.strip()[:255] if code == 0 else ""

    @staticmethod
    def _subject(worktree: Path) -> str:
        """The HEAD commit subject, so a listing shows what a release actually contains."""
        code, out, _ = _run(
            ["git", "-C", str(worktree), "log", "-1", "--no-color", "--pretty=%s"],
            timeout=30.0,
        )
        return out.strip()[:255] if code == 0 else ""


class UvWheelBuilder:
    """Build exactly one wheel from the worktree using ``uv build``."""

    def build(self, worktree: Path) -> BuildArtifact:
        out_dir = Path(tempfile.mkdtemp(prefix="repoforge-upgrade-build-"))
        code, out, err = _run(["uv", "build", "--wheel", "--out-dir", str(out_dir)], cwd=worktree)
        if code != 0:
            raise ConfigError(f"BUILD_FAILED: uv build exited {code}: {err.strip() or out.strip()}")
        wheels = sorted(out_dir.glob("*.whl"))
        if len(wheels) != 1:
            raise ConfigError(f"BUILD_AMBIGUOUS: expected one wheel, found {len(wheels)}")
        wheel = wheels[0]
        return BuildArtifact(
            wheel_path=wheel,
            build_fingerprint=_sha256_file(wheel),
            package_version=_version_from_wheel(wheel.name),
        )


class UvVenvReleaseInstaller:
    """Install a wheel into a self-contained per-release virtual environment."""

    def install(self, wheel: Path, destination: Path) -> None:
        """Build into a staging directory, then atomically rename it into place.

        Installing straight into ``releases/<sha>`` means a crashed ``uv venv``/``uv pip
        install`` leaves a partial directory with no manifest, which the next attempt can
        only report as unclaimable. Staging keeps the release directory all-or-nothing.
        """
        staging = destination.with_name(f".staging-{destination.name}-{os.getpid()}")
        _remove_tree(staging)
        try:
            self._install_into(wheel, staging)
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staging, destination)
        finally:
            _remove_tree(staging)

    def _install_into(self, wheel: Path, destination: Path) -> None:
        destination.mkdir(parents=True, exist_ok=True)
        venv = destination / _VENV
        # `--relocatable` is required, not cosmetic: a venv's console scripts normally
        # hard-code the absolute interpreter path, so staging the install and renaming it
        # into releases/<sha> would leave `venv/bin/rf` exec-ing the deleted staging path.
        # A live sandbox activation failed exactly this way ("cannot execute: No such file
        # or directory") until the venv was made relocatable.
        code, out, err = _run(["uv", "venv", "--relocatable", str(venv)], timeout=120.0)
        if code != 0:
            raise ConfigError(f"VENV_FAILED: uv venv exited {code}: {err.strip() or out.strip()}")
        code, out, err = _run(
            ["uv", "pip", "install", "--python", str(venv / "bin" / "python"), str(wheel)]
        )
        if code != 0:
            raise ConfigError(
                f"INSTALL_FAILED: uv pip install exited {code}: {err.strip() or out.strip()}"
            )


class SubprocessReleaseSmokeTester:
    """Smoke-test a candidate release by running the release itself.

    Checks, in order: the ``rf`` entry point runs (CLI/parser bootstrap), the runtime
    worker and supervisor modules import, config loading is importable, the MCP tool
    surface can be computed, and the packaged contract identity agrees with the
    in-process registry (#367). Everything runs under the candidate's own interpreter,
    so a release that cannot start -- or one built from a stale worktree -- is rejected
    before it can become ``current``.
    """

    def smoke(self, release_path: Path) -> SmokeResult:
        python = release_path / _VENV / "bin" / "python"
        if not python.is_file():
            return SmokeResult(
                ok=False, tool_surface_hash="", detail=f"missing interpreter {python}"
            )
        entry_point = release_path / _VENV / "bin" / "rf"
        if not entry_point.is_file():
            return SmokeResult(
                ok=False, tool_surface_hash="", detail=f"missing `rf` entry point {entry_point}"
            )
        code, out, err = _run([str(entry_point), "--version"], timeout=60.0)
        if code != 0:
            return SmokeResult(
                ok=False,
                tool_surface_hash="",
                detail=f"`rf --version` exited {code}: {err.strip() or out.strip() or 'no output'}",
            )
        code, out, err = _run(
            [
                str(python),
                "-c",
                # Importing these proves the runtime worker, supervisor construction path
                # and config loader are all loadable in the candidate.
                "import repoforge.config, repoforge.bootstrap, "
                "repoforge.interfaces.runtime.worker, "
                "repoforge.application.runtime.supervisor",
            ],
            timeout=60.0,
        )
        if code != 0:
            return SmokeResult(
                ok=False,
                tool_surface_hash="",
                detail=f"runtime imports failed: {err.strip() or 'no output'}",
            )
        code, out, err = _run(
            [
                str(python),
                "-c",
                "from repoforge.interfaces.mcp.server import tool_surface_hash; "
                "print(tool_surface_hash())",
            ],
            timeout=60.0,
        )
        surface = out.strip()
        if code != 0 or not surface:
            return SmokeResult(
                ok=False,
                tool_surface_hash="",
                detail=f"tool-surface probe failed: {err.strip() or 'no output'}",
            )
        probe = self._contract_probe(python)
        if probe is None:
            return SmokeResult(
                ok=False,
                tool_surface_hash=surface,
                detail="contract-identity probe failed: the release did not report a "
                "structured contract identity",
            )
        if not probe.agreement:
            detail_parts = [
                f"{field}: packaged {probe.packaged.get(field)!r} vs "
                f"computed {probe.computed.get(field)!r}"
                for field in probe.mismatched_fields
            ]
            return SmokeResult(
                ok=False,
                tool_surface_hash=surface,
                detail="RELEASE_CONTRACT_IDENTITY_MISMATCH: packaged contract identity "
                f"differs from the in-process registry ({'; '.join(detail_parts)}); "
                f"offending artifacts: {', '.join(probe.artifact_paths) or 'unknown'}",
                contract_mismatched_fields=probe.mismatched_fields,
                contract_artifact_paths=probe.artifact_paths,
                contract_packaged_identity=contract_identity_digest(probe.packaged),
                contract_computed_identity=contract_identity_digest(probe.computed),
            )
        return SmokeResult(
            ok=True,
            tool_surface_hash=surface,
            detail="entry point, runtime imports, tool surface, and contract identity verified",
            contract_identity=contract_identity_digest(probe.computed),
        )

    @staticmethod
    def _contract_probe(python: Path) -> _ContractProbe | None:
        """Run the structured contract probe inside the candidate, or ``None`` on failure."""

        code, out, _err = _run(
            [
                str(python),
                "-c",
                "import json; "
                "from repoforge.contracts.registry import release_contract_probe; "
                "print(json.dumps(release_contract_probe(), sort_keys=True))",
            ],
            timeout=60.0,
        )
        if code != 0:
            return None
        try:
            parsed = json.loads(out.strip())
        except json.JSONDecodeError:
            return None
        if not isinstance(parsed, dict):
            return None
        packaged = parsed.get("packaged")
        computed = parsed.get("computed")
        raw_mismatched = parsed.get("mismatched_fields")
        raw_paths = parsed.get("artifact_paths")
        agreement = parsed.get("agreement")
        if (
            not isinstance(packaged, dict)
            or not isinstance(computed, dict)
            or not isinstance(raw_mismatched, list)
            or not isinstance(raw_paths, list)
            or not isinstance(agreement, bool)
        ):
            return None
        mismatched = tuple(str(field) for field in raw_mismatched)
        paths = tuple(str(path) for path in raw_paths)
        return _ContractProbe(
            packaged=packaged,
            computed=computed,
            mismatched_fields=mismatched,
            artifact_paths=paths,
            agreement=agreement,
        )


class SupervisorRestarter:
    """Replace the live supervisor so it re-execs through the ``current`` symlink.

    A new release is new code, so it cannot be adopted by an in-process reload -- and
    the supervisor's control protocol deliberately supports only PING/STATUS/HEALTH/
    SHUTDOWN. Two subtleties this must respect:

    * A graceful shutdown does **not** delete the runtime record: the supervisor writes
      a ``STOPPED`` record with ``pid=None``. Waiting for the record to disappear would
      therefore misread a clean stop as a stop failure, so we wait for the *old process*
      to be gone by identity instead.
    * When the supervisor is registered with launchd, spawning our own detached process
      would take it out of the OS manager's ownership. If a kickstarter is available we
      restart the registered job instead -- but only after draining the incumbent by
      identity, because the process holding the runtime lock is not always the one
      launchd owns, and a kickstart that races it starts a second live supervisor.
    """

    def __init__(
        self,
        *,
        control: RuntimeControlClient,
        runtime: RuntimeStore,
        launcher: RuntimeLauncher,
        config_path: Path,
        correlation_id: str,
        extra_env: dict[str, str] | None = None,
        sleeper: Sleeper | None = None,
        kickstarter: SupervisorKickstarter | None = None,
        worker_reconciler: ExecutionWorkerReconciler | None = None,
        stop_timeout_seconds: float = 20.0,
        start_timeout_seconds: float = 60.0,
        poll_interval_seconds: float = 0.2,
    ) -> None:
        self._control = control
        self._runtime = runtime
        self._launcher = launcher
        self._config_path = config_path
        self._correlation_id = correlation_id
        self._extra_env = dict(extra_env or {})
        self._sleeper = sleeper
        self._kickstarter = kickstarter
        self._worker_reconciler = worker_reconciler
        self._stop_timeout = stop_timeout_seconds
        self._start_timeout = start_timeout_seconds
        self._poll = max(0.01, poll_interval_seconds)

    def _reclaim_departing(
        self, departing_release: str | None
    ) -> tuple[bool, str, dict[str, object] | None]:
        """Reclaim the departing release's execution workers; block on uncertainty.

        Returns ``(ok, detail, evidence)``. ``ok=False`` means the replacement
        supervisor must not start: a worker survived SIGKILL, a worker's identity
        could not be proven while it may still be running, the registry scan was
        truncated, or a registry record could not be read -- each recreates the
        2026-08-01 deadlock if we proceed anyway.
        """
        if self._worker_reconciler is None:
            return True, "", None
        report = self._worker_reconciler.reconcile(
            departing_releases=(
                frozenset({departing_release}) if departing_release else frozenset()
            )
        )
        evidence = report.as_dict()
        if report.survived_kill > 0:
            return (
                False,
                "STALE_EXECUTION_WORKER_RECLAMATION_FAILED: a worker of the departing "
                f"release {departing_release or 'unknown'} survived SIGKILL; refusing "
                "to start a replacement that would contend with its locks",
                evidence,
            )
        if report.possibly_alive_unproven > 0:
            return (
                False,
                "STALE_EXECUTION_WORKER_IDENTITY_UNPROVEN: "
                f"{report.possibly_alive_unproven} worker(s) of the departing release "
                f"{departing_release or 'unknown'} could not be proven dead and may "
                "still hold shared locks; refusing to start a replacement on unproven "
                "identity",
                evidence,
            )
        if not report.evidence_complete:
            if report.unreadable_record_ids:
                return (
                    False,
                    "EXECUTION_WORKER_REGISTRY_UNREADABLE_RECORDS: "
                    f"{len(report.unreadable_record_ids)} worker registry record(s) "
                    "could not be read; an unreadable record may describe a live "
                    "orphan holding shared locks; refusing to start a replacement on "
                    "incomplete evidence",
                    evidence,
                )
            return (
                False,
                "EXECUTION_WORKER_REGISTRY_SCAN_INCOMPLETE: the worker registry scan "
                "was truncated, so an orphan may be invisible; refusing to start a "
                "replacement on incomplete evidence",
                evidence,
            )
        return True, "", evidence

    def restart(self, *, departing_release: str | None = None) -> RestartOutcome:
        before = self._runtime.read()
        old_pid = before.pid if before else None
        old_identity = before.process_identity if before else None

        # Prefer the OS process manager so an upgrade never orphans the supervisor from
        # launchd. `kickstart -k` stops and restarts the registered job in one step.
        if self._kickstarter is not None and self._kickstarter.available():
            # Drain the incumbent FIRST, by identity. `kickstart -k` only replaces the
            # process launchd itself owns, so a supervisor started outside launchd -- a
            # manual `rf start`, or a leftover from an earlier release -- survives it and
            # keeps holding `runtime-single-instance`. The incoming process then dies on
            # that lock and an otherwise fine activation is reported as a failure. Observed
            # on a real activation: two supervisors alive at once, one per release.
            stopped, stop_detail = self._stop(old_pid=old_pid, old_identity=old_identity)
            if not stopped:
                return RestartOutcome(
                    ok=False,
                    detail=(
                        "could not stop the outgoing supervisor, so an OS-managed "
                        f"replacement would race it for the runtime lock: {stop_detail}"
                    ),
                )
            reclaim_ok, reclaim_detail, reclamation = self._reclaim_departing(departing_release)
            if not reclaim_ok:
                return RestartOutcome(
                    ok=False,
                    detail=reclaim_detail,
                    reclamation=reclamation,
                )
            outcome = self._kickstarter.kickstart()
            if not outcome.ok:
                return outcome
            if not self._await(
                lambda: self._live_record(exclude_pid=old_pid) is not None, self._start_timeout
            ):
                return RestartOutcome(
                    ok=False,
                    detail="OS-managed supervisor did not publish a live record after kickstart",
                )
            record = self._live_record(exclude_pid=old_pid)
            return RestartOutcome(
                ok=True,
                detail="restarted via the OS process manager",
                pid=record.pid if record else None,
                reclamation=reclamation,
            )

        stopped, stop_detail = self._stop(old_pid=old_pid, old_identity=old_identity)
        if not stopped:
            return RestartOutcome(ok=False, detail=f"could not stop the runtime: {stop_detail}")
        reclaim_ok, reclaim_detail, reclamation = self._reclaim_departing(departing_release)
        if not reclaim_ok:
            return RestartOutcome(
                ok=False,
                detail=reclaim_detail,
                reclamation=reclamation,
            )
        try:
            pid = self._launcher.start(
                self._config_path, foreground=False, extra_env=self._extra_env
            )
        except (ConfigError, OSError) as exc:
            return RestartOutcome(ok=False, detail=f"launcher failed to start: {exc}")
        # A STOPPED record from the old process is still on disk, so "a record exists"
        # proves nothing: require a live record that is not the old process.
        if not self._await(
            lambda: self._live_record(exclude_pid=old_pid) is not None, self._start_timeout
        ):
            return RestartOutcome(
                ok=False, detail="runtime did not publish a live record after restart", pid=pid
            )
        return RestartOutcome(
            ok=True, detail=f"restarted (pid {pid})", pid=pid, reclamation=reclamation
        )

    def _live_record(self, *, exclude_pid: int | None) -> RuntimeRecord | None:
        """Return the record only when it describes a live process that is not the old one."""
        record = self._runtime.read()
        if record is None or record.pid is None or record.process_identity is None:
            return None
        if record.phase in {RuntimePhase.STOPPED, RuntimePhase.FAILED}:
            return None
        if exclude_pid is not None and record.pid == exclude_pid:
            return None
        return record

    def _stop(self, *, old_pid: int | None, old_identity: str | None) -> tuple[bool, str]:
        if old_pid is None:
            return True, "no runtime was running"

        def old_process_gone() -> bool:
            current = self._runtime.read()
            if current is None:
                return True
            if current.phase in {RuntimePhase.STOPPED, RuntimePhase.FAILED}:
                return True
            # A different pid/identity means our target already exited and something
            # else owns the record.
            return current.pid != old_pid or current.process_identity != old_identity

        # A refused/unreachable shutdown is not fatal: the force-stop path below is
        # the identity-guarded fallback.
        with contextlib.suppress(ConfigError):
            self._control.request(
                ControlRequest(1, ControlCommand.SHUTDOWN, self._correlation_id),
                timeout_seconds=10.0,
            )
        if self._await(old_process_gone, self._stop_timeout):
            return True, "shutdown acknowledged"
        record = self._runtime.read()
        if record is not None and self._launcher.force_stop(record, grace_seconds=5.0):
            return True, "identity-guarded force stop"
        if old_process_gone():
            return True, "old process is gone"
        return False, "runtime still present after shutdown and force stop"

    def _await(self, predicate: Callable[[], bool], timeout_seconds: float) -> bool:
        attempts = max(1, int(timeout_seconds / self._poll))
        for _ in range(attempts):
            if predicate():
                return True
            if self._sleeper is not None:
                self._sleeper.sleep(self._poll)
        return predicate()


class RuntimeRecordReleaseObserver:
    """Derive the release the live runtime is actually serving from its own record.

    The running supervisor publishes ``executable`` (its own ``sys.executable``). When
    it was launched through the stable shim, that path lives inside
    ``<release-root>/releases/<sha>/``, so the serving release is an *observation* of
    the live process rather than a restatement of what ``current`` points at.
    """

    def __init__(self, *, runtime: RuntimeStore, releases_root: Path) -> None:
        self._runtime = runtime
        self._releases_root = releases_root

    def observe(self) -> ObservedRuntime:
        record = self._runtime.read()
        if record is None:
            return ObservedRuntime(running_release_sha=None, phase="stopped")
        # A STOPPED/FAILED record keeps `executable` but has no live process, so deriving
        # a serving release from it would claim a runtime that is not running.
        live = record.pid is not None and record.phase not in {
            RuntimePhase.STOPPED,
            RuntimePhase.FAILED,
        }
        # Source of truth is the identity the process itself published. `executable` is
        # NOT usable for this: a relocatable venv's python is a symlink to a shared
        # uv-managed interpreter, so resolving it escapes the release directory entirely.
        return ObservedRuntime(
            running_release_sha=record.running_release_sha if live else None,
            phase=record.phase.value,
            pid=record.pid,
            executable=record.executable,
            tool_surface_hash=record.tool_surface_hash or None,
            last_error_code=record.last_error_code,
            fail_closed_since=record.fail_closed_since,
        )

    def release_of_executable(self, executable: str | None) -> str | None:
        """Supporting evidence only -- never the identity source.

        Deliberately does NOT resolve symlinks: a relocatable venv's ``bin/python`` points
        at a shared uv-managed interpreter, so resolving would leave the release tree.
        Kept for diagnostics where a legacy record has no published identity.
        """
        if not executable:
            return None
        try:
            relative = Path(executable).relative_to(self._releases_root)
        except ValueError:
            return None
        return relative.parts[0] if relative.parts else None


class SupervisorHealthProbe:
    """Sample runtime health via the supervisor control socket HEALTH command."""

    def __init__(self, client: RuntimeControlClient, *, correlation_id: str) -> None:
        self._client = client
        self._correlation_id = correlation_id

    def sample(self) -> HealthSample:
        try:
            response = self._client.request(
                ControlRequest(1, ControlCommand.HEALTH, self._correlation_id),
                timeout_seconds=5.0,
            )
        except ConfigError as exc:
            return HealthSample(healthy=False, detail=f"health probe unreachable: {exc}")
        healthy = response.ok and response.status == "healthy"
        return HealthSample(healthy=healthy, detail=response.message or response.status)


def _remove_tree(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _version_from_wheel(name: str) -> str:
    parts = name.split("-")
    if len(parts) < 2 or not parts[1]:
        raise ConfigError(f"BUILD_VERSION_UNKNOWN: cannot parse version from {name}")
    return parts[1]


def _first_lines(text: str, limit: int) -> str:
    lines = text.splitlines()
    if len(lines) <= limit:
        return text
    return "\n".join(lines[:limit]) + f"\n... (+{len(lines) - limit} more)"
