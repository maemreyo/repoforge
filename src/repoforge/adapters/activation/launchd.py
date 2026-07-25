"""launchd LaunchAgent adapter that makes the RepoForge supervisor OS-resident.

The plist runs the *stable launcher* (``<release-root>/bin/rf``) in foreground -- which
starts the supervisor worker -- under ``KeepAlive``, so launchd relaunches it after a
crash and starts it at login. Because the launcher resolves through ``current``, the
agent always runs whichever release is active without the plist ever changing.
"""

from __future__ import annotations

import os
import plistlib
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ...domain.errors import ConfigError
from ...ports.activation import RestartOutcome
from ...ports.process_supervisor import RegistrarResult, RegistrarStatus

DEFAULT_LABEL = "dev.repoforge.supervisor"
"""Label for the DEFAULT release root only; other roots must namespace their own."""


@dataclass(frozen=True, slots=True)
class LaunchAgentSpec:
    label: str
    launcher_path: Path
    config_path: Path
    stdout_path: Path
    stderr_path: Path
    inherited_env: dict[str, str]


def render_launch_agent(spec: LaunchAgentSpec) -> bytes:
    """Render the LaunchAgent plist. Pure: no filesystem or launchctl side effects."""
    # `launcher_path` is the supervisor shim, which execs the worker: launchd then owns
    # the supervisor process itself rather than a CLI wrapper whose child is the
    # supervisor (which would make the launchd pid differ from the runtime record pid).
    program_arguments = [
        str(spec.launcher_path),
        "--config",
        str(spec.config_path),
    ]
    payload: dict[str, object] = {
        "Label": spec.label,
        "ProgramArguments": program_arguments,
        "RunAtLoad": True,
        # Relaunch on crash but not in a tight loop after a clean stop.
        "KeepAlive": {"SuccessfulExit": False, "Crashed": True},
        "ProcessType": "Background",
        "StandardOutPath": str(spec.stdout_path),
        "StandardErrorPath": str(spec.stderr_path),
    }
    if spec.inherited_env:
        payload["EnvironmentVariables"] = dict(sorted(spec.inherited_env.items()))
    return plistlib.dumps(payload, sort_keys=True)


class CommandRunner(Protocol):
    def __call__(self, argv: list[str]) -> tuple[int, str]: ...


def _default_runner(argv: list[str]) -> tuple[int, str]:
    try:
        completed = subprocess.run(argv, capture_output=True, text=True, check=False, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc)
    return completed.returncode, (completed.stdout + completed.stderr).strip()


class LaunchdRegistrar:
    """Install/uninstall/inspect the supervisor LaunchAgent via ``launchctl``."""

    def __init__(
        self,
        *,
        spec: LaunchAgentSpec,
        agents_dir: Path,
        uid: int | None = None,
        runner: CommandRunner = _default_runner,
    ) -> None:
        self._spec = spec
        self._plist_path = agents_dir / f"{spec.label}.plist"
        self._uid = uid if uid is not None else os.getuid()
        self._run = runner

    @property
    def plist_path(self) -> Path:
        return self._plist_path

    def install(self) -> RegistrarResult:
        self._plist_path.parent.mkdir(parents=True, exist_ok=True)
        self._spec.stdout_path.parent.mkdir(parents=True, exist_ok=True)
        self._plist_path.write_bytes(render_launch_agent(self._spec))
        self._plist_path.chmod(0o644)
        # Replace any prior registration so the new plist is what runs.
        self._run(["launchctl", "bootout", self._domain(), str(self._plist_path)])
        code, detail = self._run(["launchctl", "bootstrap", self._domain(), str(self._plist_path)])
        if code != 0:
            raise ConfigError(
                f"LAUNCHD_BOOTSTRAP_FAILED: launchctl exited {code}: {detail or 'no output'}"
            )
        return RegistrarResult(
            status="installed",
            detail="LaunchAgent installed and bootstrapped; supervisor is now OS-resident.",
            unit_path=str(self._plist_path),
        )

    def uninstall(self) -> RegistrarResult:
        code, detail = self._run(["launchctl", "bootout", self._domain(), str(self._plist_path)])
        removed = False
        if self._plist_path.is_file():
            self._plist_path.unlink()
            removed = True
        return RegistrarResult(
            status="uninstalled" if removed or code == 0 else "not_registered",
            detail=detail or "LaunchAgent removed.",
            unit_path=str(self._plist_path),
        )

    def available(self) -> bool:
        """True when this supervisor is a registered, loaded launchd job."""
        return self.status().loaded

    def kickstart(self) -> RestartOutcome:
        """Stop-and-restart the registered job so launchd keeps owning the supervisor."""
        code, detail = self._run(
            ["launchctl", "kickstart", "-k", f"{self._domain()}/{self._spec.label}"]
        )
        if code != 0:
            return RestartOutcome(
                ok=False, detail=f"launchctl kickstart exited {code}: {detail or 'no output'}"
            )
        return RestartOutcome(ok=True, detail="kickstarted the launchd job")

    def status(self) -> RegistrarStatus:
        registered = self._plist_path.is_file()
        code, detail = self._run(["launchctl", "print", f"{self._domain()}/{self._spec.label}"])
        return RegistrarStatus(
            registered=registered,
            loaded=code == 0,
            detail="loaded" if code == 0 else (detail or "not loaded"),
            unit_path=str(self._plist_path),
        )

    def _domain(self) -> str:
        return f"gui/{self._uid}"
