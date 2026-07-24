from __future__ import annotations

import plistlib
from pathlib import Path

import pytest

from repoforge.adapters.activation.launchd import (
    DEFAULT_LABEL,
    LaunchAgentSpec,
    LaunchdRegistrar,
    render_launch_agent,
)
from repoforge.domain.errors import ConfigError


def _spec(tmp_path: Path) -> LaunchAgentSpec:
    return LaunchAgentSpec(
        label=DEFAULT_LABEL,
        launcher_path=tmp_path / "bin" / "rf",
        config_path=tmp_path / "config.toml",
        stdout_path=tmp_path / "logs" / "out.log",
        stderr_path=tmp_path / "logs" / "err.log",
        inherited_env={"PATH": "/usr/bin"},
    )


def test_render_launch_agent_runs_the_stable_launcher_in_foreground(tmp_path: Path) -> None:
    payload = plistlib.loads(render_launch_agent(_spec(tmp_path)))
    assert payload["Label"] == DEFAULT_LABEL
    # `start` (not `start --background`) runs the supervisor in foreground so
    # launchd can KeepAlive it.
    assert payload["ProgramArguments"] == [
        str(tmp_path / "bin" / "rf"),
        "--config",
        str(tmp_path / "config.toml"),
        "start",
    ]
    assert payload["RunAtLoad"] is True
    assert payload["KeepAlive"] == {"SuccessfulExit": False, "Crashed": True}
    assert payload["EnvironmentVariables"] == {"PATH": "/usr/bin"}


def test_install_writes_the_plist_and_bootstraps_the_domain(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> tuple[int, str]:
        calls.append(argv)
        return 0, ""

    agents_dir = tmp_path / "LaunchAgents"
    registrar = LaunchdRegistrar(
        spec=_spec(tmp_path), agents_dir=agents_dir, uid=501, runner=runner
    )
    result = registrar.install()

    plist = agents_dir / f"{DEFAULT_LABEL}.plist"
    assert plist.is_file()
    assert result.status == "installed"
    # Prior registration is booted out before bootstrapping the new plist.
    assert calls[0][:2] == ["launchctl", "bootout"]
    assert calls[1][:2] == ["launchctl", "bootstrap"]
    assert "gui/501" in calls[1]


def test_install_raises_when_bootstrap_fails(tmp_path: Path) -> None:
    def runner(argv: list[str]) -> tuple[int, str]:
        if argv[1] == "bootstrap":
            return 5, "Input/output error"
        return 0, ""

    registrar = LaunchdRegistrar(
        spec=_spec(tmp_path), agents_dir=tmp_path / "LaunchAgents", uid=501, runner=runner
    )
    with pytest.raises(ConfigError, match="LAUNCHD_BOOTSTRAP_FAILED"):
        registrar.install()


def test_uninstall_removes_the_plist(tmp_path: Path) -> None:
    registrar = LaunchdRegistrar(
        spec=_spec(tmp_path),
        agents_dir=tmp_path / "LaunchAgents",
        uid=501,
        runner=lambda argv: (0, ""),
    )
    registrar.install()
    assert registrar.plist_path.is_file()

    result = registrar.uninstall()
    assert result.status == "uninstalled"
    assert not registrar.plist_path.is_file()
