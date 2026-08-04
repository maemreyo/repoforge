from __future__ import annotations

import plistlib
from pathlib import Path

import pytest

from repoforge.adapters.activation.launchd import (
    LaunchAgentSpec,
    LaunchdRegistrar,
    render_launch_agent,
)
from repoforge.domain.activation import SUPERVISOR_AGENT_LABEL
from repoforge.domain.errors import ConfigError


def _spec(tmp_path: Path) -> LaunchAgentSpec:
    return LaunchAgentSpec(
        label=SUPERVISOR_AGENT_LABEL,
        launcher_path=tmp_path / "bin" / "rf",
        config_path=tmp_path / "config.toml",
        stdout_path=tmp_path / "logs" / "out.log",
        stderr_path=tmp_path / "logs" / "err.log",
        inherited_env={"PATH": "/usr/bin"},
    )


def test_render_launch_agent_execs_the_supervisor_directly(tmp_path: Path) -> None:
    """Round-4 finding 2: launchd must own the supervisor pid, not a CLI wrapper.

    The agent runs the supervisor shim, which ``exec``s the worker, so the pid launchd
    supervises is the same pid the runtime record publishes. Passing `start` here would
    put a CLI wrapper in between.
    """
    payload = plistlib.loads(render_launch_agent(_spec(tmp_path)))
    assert payload["Label"] == SUPERVISOR_AGENT_LABEL
    assert payload["ProgramArguments"] == [
        str(tmp_path / "bin" / "rf"),
        "--config",
        str(tmp_path / "config.toml"),
    ]
    assert "start" not in payload["ProgramArguments"]
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

    plist = agents_dir / f"{SUPERVISOR_AGENT_LABEL}.plist"
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


# ---------------------------------------------------------------------------
# F-012 OS-managed path: the replacement permit is transported through the job
# definition (launchd owns the replacement environment), then scrubbed.
# ---------------------------------------------------------------------------


def _env_of(plist_path: Path) -> dict[str, object]:
    return plistlib.loads(plist_path.read_bytes()).get("EnvironmentVariables", {})


def test_stage_replacement_env_injects_only_the_permit_key_without_launchctl(
    tmp_path: Path,
) -> None:
    """Staging writes the permit into the plist on disk with NO launchctl side effect."""
    from repoforge.ports.admission_epoch import ADMISSION_PERMIT_ENV

    calls: list[list[str]] = []

    def runner(argv: list[str]) -> tuple[int, str]:
        calls.append(argv)
        return 0, ""

    agents_dir = tmp_path / "LaunchAgents"
    registrar = LaunchdRegistrar(
        spec=_spec(tmp_path), agents_dir=agents_dir, uid=501, runner=runner
    )
    registrar.install()
    before = registrar.plist_path.stat()
    calls_before_stage = len(calls)

    ok, detail = registrar.stage_replacement_env({ADMISSION_PERMIT_ENV: "tok-123"})

    assert ok is True, detail
    assert len(calls) == calls_before_stage, "staging must not touch launchctl"
    env = _env_of(registrar.plist_path)
    assert env == {"PATH": "/usr/bin", ADMISSION_PERMIT_ENV: "tok-123"}
    after = registrar.plist_path.stat()
    assert after.st_mode & 0o777 == before.st_mode & 0o777 == 0o644
    assert (after.st_uid, after.st_gid) == (before.st_uid, before.st_gid)


def test_stage_replacement_env_fails_closed_when_there_is_no_plist(tmp_path: Path) -> None:
    from repoforge.ports.admission_epoch import ADMISSION_PERMIT_ENV

    registrar = LaunchdRegistrar(
        spec=_spec(tmp_path),
        agents_dir=tmp_path / "LaunchAgents",
        uid=501,
        runner=lambda argv: (0, ""),
    )
    ok, detail = registrar.stage_replacement_env({ADMISSION_PERMIT_ENV: "tok"})
    assert ok is False
    assert "LAUNCHD_PLIST_MISSING" in detail


def test_bootstrap_replacement_success_does_not_kickstart(tmp_path: Path) -> None:
    """The bootstrap is the launch: bootout + bootstrap, and never a kickstart."""
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> tuple[int, str]:
        calls.append(argv)
        return 0, ""

    agents_dir = tmp_path / "LaunchAgents"
    registrar = LaunchdRegistrar(
        spec=_spec(tmp_path), agents_dir=agents_dir, uid=501, runner=runner
    )
    registrar.install()

    outcome = registrar.bootstrap_replacement()

    assert outcome.ok is True, outcome.detail
    assert "RunAtLoad launched the replacement" in outcome.detail
    # install: bootout+bootstrap; replacement: bootout+bootstrap. No kickstart ever.
    assert [argv[1] for argv in calls] == [
        "bootout",
        "bootstrap",
        "bootout",
        "bootstrap",
    ]
    assert "kickstart" not in [argv[1] for argv in calls]


def test_bootstrap_replacement_bootout_failure_restores_disk_and_does_not_bootstrap(
    tmp_path: Path,
) -> None:
    """A bootout failure keeps the job LOADED: no bootstrap, disk restored to match."""
    from repoforge.ports.admission_epoch import ADMISSION_PERMIT_ENV

    calls: list[list[str]] = []

    def runner(argv: list[str]) -> tuple[int, str]:
        calls.append(argv)
        # install() bootout/bootstrap succeed; the replacement bootout (index 2) fails.
        if len(calls) == 3:
            return 1, "bootout refused"
        return 0, ""

    agents_dir = tmp_path / "LaunchAgents"
    registrar = LaunchdRegistrar(
        spec=_spec(tmp_path), agents_dir=agents_dir, uid=501, runner=runner
    )
    registrar.install()
    staged_ok, _ = registrar.stage_replacement_env({ADMISSION_PERMIT_ENV: "tok"})
    assert staged_ok

    outcome = registrar.bootstrap_replacement()

    assert outcome.ok is False
    assert "LAUNCHD_BOOTOUT_FAILED" in outcome.detail
    assert "LOADED" in outcome.detail, "the job was never unloaded"
    assert "restored on disk" in outcome.detail
    # No bootstrap after the failed bootout.
    assert len(calls) == 3
    # The on-disk definition is back to the original (permit absent).
    assert ADMISSION_PERMIT_ENV not in _env_of(registrar.plist_path)


def test_bootstrap_replacement_bootstrap_failure_restores_disk_without_rebootstrap(
    tmp_path: Path,
) -> None:
    """A bootstrap failure leaves the job UNLOADED and never re-bootstraps the old plist.

    Re-bootstrapping the previous definition here would launch a supervisor that
    cannot pass the CLOSING worker-admission fence (it carries no valid replacement
    permit); recovery belongs to the outer rollback protocol with a fresh permit.
    """
    from repoforge.ports.admission_epoch import ADMISSION_PERMIT_ENV

    calls: list[list[str]] = []

    def runner(argv: list[str]) -> tuple[int, str]:
        calls.append(argv)
        # install() bootout+bootstrap succeed; the replacement bootstrap (index 3) fails.
        if len(calls) == 4:
            return 7, "bad plist"
        return 0, ""

    agents_dir = tmp_path / "LaunchAgents"
    registrar = LaunchdRegistrar(
        spec=_spec(tmp_path), agents_dir=agents_dir, uid=501, runner=runner
    )
    registrar.install()
    staged_ok, _ = registrar.stage_replacement_env({ADMISSION_PERMIT_ENV: "tok"})
    assert staged_ok

    outcome = registrar.bootstrap_replacement()

    assert outcome.ok is False
    assert "LAUNCHD_BOOTSTRAP_FAILED" in outcome.detail
    assert "UNLOADED" in outcome.detail, "the old definition was already booted out"
    assert "restored on disk" in outcome.detail
    # The restore is disk-only: no second bootstrap call after the failed one.
    assert len(calls) == 4
    # The on-disk definition no longer carries the staged permit.
    assert ADMISSION_PERMIT_ENV not in _env_of(registrar.plist_path)


def test_scrub_replacement_env_removes_only_the_given_keys_without_reloading(
    tmp_path: Path,
) -> None:
    from repoforge.ports.admission_epoch import ADMISSION_PERMIT_ENV

    calls: list[list[str]] = []

    def runner(argv: list[str]) -> tuple[int, str]:
        calls.append(argv)
        return 0, ""

    agents_dir = tmp_path / "LaunchAgents"
    registrar = LaunchdRegistrar(
        spec=_spec(tmp_path), agents_dir=agents_dir, uid=501, runner=runner
    )
    registrar.install()
    staged_ok, _ = registrar.stage_replacement_env({ADMISSION_PERMIT_ENV: "tok", "KEEP_ME": "1"})
    assert staged_ok
    calls_before_scrub = len(calls)

    ok, detail = registrar.scrub_replacement_env((ADMISSION_PERMIT_ENV,))

    assert ok is True, detail
    assert len(calls) == calls_before_scrub, "scrub must not reload (no launchctl calls)"
    env = _env_of(registrar.plist_path)
    assert env == {"PATH": "/usr/bin", "KEEP_ME": "1"}
    assert ADMISSION_PERMIT_ENV not in env
