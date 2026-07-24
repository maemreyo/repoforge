"""Concrete build/install/smoke/inspect adapters for the upgrade pipeline.

These are thin shells over ``git`` and ``uv`` that run in the launcher/CLI context.
The orchestration that uses them (``UpgradeService``) is unit-tested with fakes; these
adapters carry only the subprocess wiring.
"""

from __future__ import annotations

import contextlib
import hashlib
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path

from ...domain.errors import ConfigError
from ...domain.runtime import ControlCommand, ControlRequest
from ...ports.activation import (
    BuildArtifact,
    HealthSample,
    ObservedRuntime,
    RestartOutcome,
    SmokeResult,
    WorktreeState,
)
from ...ports.runtime_control import RuntimeControlClient, RuntimeLauncher, RuntimeStore
from ...ports.sleeper import Sleeper

_VENV = "venv"


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
        )


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
        destination.mkdir(parents=True, exist_ok=True)
        venv = destination / _VENV
        code, out, err = _run(["uv", "venv", str(venv)], timeout=120.0)
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
    """Smoke-test a candidate release by running its own interpreter."""

    def smoke(self, release_path: Path) -> SmokeResult:
        python = release_path / _VENV / "bin" / "python"
        if not python.is_file():
            return SmokeResult(
                ok=False, tool_surface_hash="", detail=f"missing interpreter {python}"
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
        return SmokeResult(ok=True, tool_surface_hash=surface, detail="tool surface computed")


class SupervisorRestarter:
    """Replace the live supervisor so it re-execs through the ``current`` symlink.

    A new release is new code, so it cannot be adopted by an in-process reload -- and
    the supervisor's control protocol deliberately supports only PING/STATUS/HEALTH/
    SHUTDOWN. So we stop the running supervisor (SHUTDOWN, falling back to an
    identity-guarded process-group kill) and start it again through the launcher,
    which resolves the release via ``current``.
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
        stop_timeout_seconds: float = 20.0,
        start_timeout_seconds: float = 20.0,
        poll_interval_seconds: float = 0.2,
    ) -> None:
        self._control = control
        self._runtime = runtime
        self._launcher = launcher
        self._config_path = config_path
        self._correlation_id = correlation_id
        self._extra_env = dict(extra_env or {})
        self._sleeper = sleeper
        self._stop_timeout = stop_timeout_seconds
        self._start_timeout = start_timeout_seconds
        self._poll = max(0.01, poll_interval_seconds)

    def restart(self) -> RestartOutcome:
        stopped, stop_detail = self._stop()
        if not stopped:
            return RestartOutcome(ok=False, detail=f"could not stop the runtime: {stop_detail}")
        try:
            pid = self._launcher.start(
                self._config_path, foreground=False, extra_env=self._extra_env
            )
        except (ConfigError, OSError) as exc:
            return RestartOutcome(ok=False, detail=f"launcher failed to start: {exc}")
        if not self._await(lambda: self._runtime.read() is not None, self._start_timeout):
            return RestartOutcome(
                ok=False, detail="runtime did not publish state after restart", pid=pid
            )
        return RestartOutcome(ok=True, detail=f"restarted (pid {pid})", pid=pid)

    def _stop(self) -> tuple[bool, str]:
        record = self._runtime.read()
        if record is None:
            return True, "no runtime was running"
        # A refused/unreachable shutdown is not fatal: the force-stop path below is
        # the identity-guarded fallback.
        with contextlib.suppress(ConfigError):
            self._control.request(
                ControlRequest(1, ControlCommand.SHUTDOWN, self._correlation_id),
                timeout_seconds=10.0,
            )
        if self._await(lambda: self._runtime.read() is None, self._stop_timeout):
            return True, "shutdown acknowledged"
        if self._launcher.force_stop(record, grace_seconds=5.0):
            return True, "identity-guarded force stop"
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
        return ObservedRuntime(
            running_release_sha=self._release_of(record.executable),
            phase=record.phase.value,
            pid=record.pid,
            executable=record.executable,
            tool_surface_hash=record.tool_surface_hash or None,
        )

    def _release_of(self, executable: str | None) -> str | None:
        if not executable:
            return None
        try:
            relative = Path(executable).resolve().relative_to(self._releases_root.resolve())
        except (ValueError, OSError):
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
