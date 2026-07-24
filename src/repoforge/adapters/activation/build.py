"""Concrete build/install/smoke/inspect adapters for the upgrade pipeline.

These are thin shells over ``git`` and ``uv`` that run in the launcher/CLI context.
The orchestration that uses them (``UpgradeService``) is unit-tested with fakes; these
adapters carry only the subprocess wiring.
"""

from __future__ import annotations

import hashlib
import subprocess
import tempfile
from pathlib import Path

from ...domain.errors import ConfigError
from ...domain.runtime import ControlCommand, ControlRequest
from ...ports.activation import BuildArtifact, SmokeResult, WorktreeState
from ...ports.runtime_control import RuntimeControlClient

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


class SupervisorControlReloader:
    """Ask the running supervisor to adopt whatever ``current`` now points at."""

    def __init__(self, client: RuntimeControlClient, *, correlation_id: str) -> None:
        self._client = client
        self._correlation_id = correlation_id

    def reload(self) -> bool:
        try:
            response = self._client.request(
                ControlRequest(1, ControlCommand.RELOAD, self._correlation_id),
                timeout_seconds=10.0,
            )
        except ConfigError:
            return False
        return response.ok


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
