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
    """The bootstrap is the launch: probe, bootout, verify, bootstrap -- never kickstart."""
    calls: list[list[str]] = []
    state = {"loaded": True}

    def runner(argv: list[str]) -> tuple[int, str]:
        calls.append(argv)
        if argv[1] == "print":
            # The domain probe always answers; the service probe reflects the job state.
            if argv[2] == "gui/501":
                return 0, ""
            return (0, "") if state["loaded"] else (1, "Could not find service")
        if argv[1] == "bootout":
            state["loaded"] = False
            return 0, ""
        if argv[1] == "bootstrap":
            state["loaded"] = True
            return 0, ""
        return 0, ""

    agents_dir = tmp_path / "LaunchAgents"
    registrar = LaunchdRegistrar(
        spec=_spec(tmp_path), agents_dir=agents_dir, uid=501, runner=runner
    )
    registrar.install()

    outcome = registrar.bootstrap_replacement()

    assert outcome.ok is True, outcome.detail
    assert "RunAtLoad launched the replacement" in outcome.detail
    # install: bootout+bootstrap; replacement: probe, bootout, verify, bootstrap.
    assert [argv[1] for argv in calls] == [
        "bootout",
        "bootstrap",
        "print",
        "bootout",
        "print",
        "print",
        "bootstrap",
    ]
    assert "kickstart" not in [argv[1] for argv in calls]


def test_bootstrap_replacement_bootout_failure_restores_disk_and_does_not_bootstrap(
    tmp_path: Path,
) -> None:
    """A bootout failure with the job still LOADED: no bootstrap, disk restored to match."""
    from repoforge.ports.admission_epoch import ADMISSION_PERMIT_ENV

    calls: list[list[str]] = []
    state = {"loaded": True, "bootouts": 0}

    def runner(argv: list[str]) -> tuple[int, str]:
        calls.append(argv)
        if argv[1] == "print":
            if argv[2] == "gui/501":
                return 0, ""
            return (0, "") if state["loaded"] else (1, "Could not find service")
        if argv[1] == "bootout":
            state["bootouts"] += 1
            if state["bootouts"] == 2:  # the replacement bootout fails; the job stays loaded
                return 1, "bootout refused"
            state["loaded"] = False
            return 0, ""
        if argv[1] == "bootstrap":
            state["loaded"] = True
            return 0, ""
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
    assert "LOADED" in outcome.detail, "the re-probe shows the job is still loaded"
    assert "restored on disk" in outcome.detail
    # probe + failed bootout + re-probe: no bootstrap after the failed bootout.
    assert [argv[1] for argv in calls] == [
        "bootout",
        "bootstrap",
        "print",
        "bootout",
        "print",
    ]
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
    state = {"loaded": True, "bootstraps": 0}

    def runner(argv: list[str]) -> tuple[int, str]:
        calls.append(argv)
        if argv[1] == "print":
            if argv[2] == "gui/501":
                return 0, ""
            return (0, "") if state["loaded"] else (1, "Could not find service")
        if argv[1] == "bootout":
            state["loaded"] = False
            return 0, ""
        if argv[1] == "bootstrap":
            state["bootstraps"] += 1
            if state["bootstraps"] == 2:  # the replacement bootstrap fails
                return 7, "bad plist"
            state["loaded"] = True
            return 0, ""
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
    # probe, bootout, verify, then the ONE failed bootstrap: no re-bootstrap.
    assert [argv[1] for argv in calls] == [
        "bootout",
        "bootstrap",
        "print",
        "bootout",
        "print",
        "print",
        "bootstrap",
    ]
    # The on-disk definition no longer carries the staged permit.
    assert ADMISSION_PERMIT_ENV not in _env_of(registrar.plist_path)


def test_bootstrap_replacement_skips_bootout_when_the_job_is_already_unloaded(
    tmp_path: Path,
) -> None:
    """P0: a rollback after a failed candidate bootstrap starts UNLOADED but registered.

    Booting the already-gone job out again would fail with "service not found" even
    though the precondition is met; the bootstrap must skip the bootout and go
    straight to bootstrapping the staged definition.
    """
    from repoforge.ports.admission_epoch import ADMISSION_PERMIT_ENV

    calls: list[list[str]] = []
    state = {"loaded": True}

    def runner(argv: list[str]) -> tuple[int, str]:
        calls.append(argv)
        if argv[1] == "print":
            if argv[2] == "gui/501":
                return 0, ""
            return (0, "") if state["loaded"] else (1, "Could not find service")
        if argv[1] == "bootout":
            state["loaded"] = False
            return 0, ""
        if argv[1] == "bootstrap":
            state["loaded"] = True
            return 0, ""
        return 0, ""

    agents_dir = tmp_path / "LaunchAgents"
    registrar = LaunchdRegistrar(
        spec=_spec(tmp_path), agents_dir=agents_dir, uid=501, runner=runner
    )
    registrar.install()
    staged_ok, _ = registrar.stage_replacement_env({ADMISSION_PERMIT_ENV: "tok"})
    assert staged_ok
    state["loaded"] = False  # the previous handoff's failed bootstrap left it unloaded

    outcome = registrar.bootstrap_replacement()

    assert outcome.ok is True, outcome.detail
    assert "RunAtLoad launched the replacement" in outcome.detail
    # install: bootout+bootstrap; replacement: probe + bootstrap, NO bootout.
    assert [argv[1] for argv in calls] == [
        "bootout",
        "bootstrap",
        "print",
        "print",
        "bootstrap",
    ]


def test_bootstrap_replacement_proceeds_when_bootout_fails_but_the_job_is_unloaded(
    tmp_path: Path,
) -> None:
    """P0: the job state is probed, never inferred from the bootout exit code alone.

    A bootout that exits nonzero while a re-probe shows the job UNLOADED (it exited
    or was removed during the attempt) still proceeds to bootstrap the staged
    definition.
    """
    from repoforge.ports.admission_epoch import ADMISSION_PERMIT_ENV

    calls: list[list[str]] = []
    state = {"loaded": True, "bootouts": 0}

    def runner(argv: list[str]) -> tuple[int, str]:
        calls.append(argv)
        if argv[1] == "print":
            if argv[2] == "gui/501":
                return 0, ""
            return (0, "") if state["loaded"] else (1, "Could not find service")
        if argv[1] == "bootout":
            state["bootouts"] += 1
            if state["bootouts"] == 2:
                state["loaded"] = False
                return 1, "Bootstrap failed: 5: Input/output error"
            state["loaded"] = False
            return 0, ""
        if argv[1] == "bootstrap":
            state["loaded"] = True
            return 0, ""
        return 0, ""

    agents_dir = tmp_path / "LaunchAgents"
    registrar = LaunchdRegistrar(
        spec=_spec(tmp_path), agents_dir=agents_dir, uid=501, runner=runner
    )
    registrar.install()
    staged_ok, _ = registrar.stage_replacement_env({ADMISSION_PERMIT_ENV: "tok"})
    assert staged_ok

    outcome = registrar.bootstrap_replacement()

    assert outcome.ok is True, outcome.detail
    assert "RunAtLoad launched the replacement" in outcome.detail
    # The failed bootout is followed by a re-probe (unloaded) and then the bootstrap.
    assert [argv[1] for argv in calls] == [
        "bootout",
        "bootstrap",
        "print",
        "bootout",
        "print",
        "print",
        "bootstrap",
    ]


def test_bootstrap_replacement_fails_closed_when_the_job_state_is_unknown(
    tmp_path: Path,
) -> None:
    """The job state is probed, never guessed: an unreachable domain fails closed."""
    from repoforge.ports.admission_epoch import ADMISSION_PERMIT_ENV

    calls: list[list[str]] = []

    def runner(argv: list[str]) -> tuple[int, str]:
        calls.append(argv)
        if argv[1] == "print":
            return 1, "Bootstrap failed: 5: Input/output error"
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
    assert "LAUNCHD_STATE_UNKNOWN" in outcome.detail
    # probe + domain probe, then fail closed: no bootout, no bootstrap.
    assert [argv[1] for argv in calls] == ["bootout", "bootstrap", "print", "print"]


def test_registered_reports_the_disk_definition_even_when_the_job_is_unloaded(
    tmp_path: Path,
) -> None:
    """P0: the OS-managed selector keys on REGISTERED (on disk), not on loaded."""
    calls: list[list[str]] = []
    state = {"loaded": True}

    def runner(argv: list[str]) -> tuple[int, str]:
        calls.append(argv)
        if argv[1] == "print":
            if argv[2] == "gui/501":
                return 0, ""
            return (0, "") if state["loaded"] else (1, "Could not find service")
        if argv[1] == "bootout":
            state["loaded"] = False
            return 0, ""
        if argv[1] == "bootstrap":
            state["loaded"] = True
            return 0, ""
        return 0, ""

    agents_dir = tmp_path / "LaunchAgents"
    registrar = LaunchdRegistrar(
        spec=_spec(tmp_path), agents_dir=agents_dir, uid=501, runner=runner
    )
    registrar.install()
    assert registrar.registered() is True
    assert registrar.loaded() is True

    state["loaded"] = False  # e.g. a failed candidate bootstrap left the job unloaded
    assert registrar.registered() is True, "the definition is still registered on disk"
    assert registrar.loaded() is False


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
