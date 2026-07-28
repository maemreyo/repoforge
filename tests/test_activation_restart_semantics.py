"""Round-2 review regressions: real stop/start semantics and new-binary adoption.

These exercise the production `SupervisorRestarter`, `ReleaseAwareRuntimeLauncher`,
`RuntimeRecordReleaseObserver` and a real `JsonRuntimeStore` -- the boundary the
earlier fake-only tests could not see past.
"""

from __future__ import annotations

import dataclasses
import os
from pathlib import Path

from repoforge.adapters.activation.build import RuntimeRecordReleaseObserver, SupervisorRestarter
from repoforge.adapters.activation.launcher import ReleaseAwareRuntimeLauncher
from repoforge.adapters.runtime import JsonRuntimeStore
from repoforge.adapters.runtime.state_store import process_identity
from repoforge.domain.errors import ConfigError
from repoforge.domain.runtime import (
    ControlResponse,
    RuntimePhase,
    RuntimeRecord,
)
from repoforge.ports.activation import RestartOutcome

_IDENTITY_OLD = "a" * 64
_IDENTITY_NEW = "b" * 64
_SURFACE = "c" * 64


def _record(
    *,
    phase: RuntimePhase,
    pid: int | None,
    identity: str | None,
    executable: str | None = None,
) -> RuntimeRecord:
    return RuntimeRecord(
        protocol_version=1,
        phase=phase,
        pid=pid,
        process_identity=identity,
        active_generation=1 if pid else None,
        accepted_generation=1,
        tunnel_profile="p",
        tunnel_profile_fingerprint=_SURFACE,
        tool_surface_hash=_SURFACE,
        started_at="2026-07-25T00:00:00+00:00",
        updated_at="2026-07-25T00:00:00+00:00",
        correlation_id="c" * 24,
        child_pid=999 if pid else None,
        child_process_identity=_IDENTITY_NEW if pid else None,
        executable=executable,
    )


class _Store:
    """Faithful in-memory RuntimeStore.

    `JsonRuntimeStore.read()` validates the recorded pid against a *live* process, so it
    cannot hold synthetic records. This keeps the production semantics that matter here
    -- a graceful shutdown REPLACES the record with STOPPED rather than deleting it --
    while the code under test stays the real `SupervisorRestarter`.
    (`test_a_graceful_stop_really_leaves_the_record_on_disk` pins that premise against
    the real JSON store.)
    """

    def __init__(self) -> None:
        self._record: RuntimeRecord | None = None

    def read(self) -> RuntimeRecord | None:
        return self._record

    def write(self, record: RuntimeRecord) -> None:
        self._record = record

    def clear(self, *, expected_pid: int | None = None) -> None:
        self._record = None


class _Sleeper:
    def sleep(self, seconds: float) -> None:  # no real waiting in tests
        return None


class _Control:
    """Control fake that flips the store to a graceful STOPPED record, as production does."""

    def __init__(self, store, *, stopped_executable: str | None = None) -> None:
        self._store = store
        self._stopped_executable = stopped_executable

    def request(self, request, *, timeout_seconds: float = 10.0) -> ControlResponse:
        # Exactly what the supervisor does on SHUTDOWN: write STOPPED, do NOT delete.
        self._store.write(
            _record(
                phase=RuntimePhase.STOPPED,
                pid=None,
                identity=None,
                executable=self._stopped_executable,
            )
        )
        return ControlResponse(1, True, request.correlation_id, "stopping")


class _RecordingLauncher:
    """Captures argv-equivalents and publishes a live record for the new process."""

    def __init__(self, store, *, new_pid: int = 4242) -> None:
        self._store = store
        self._new_pid = new_pid
        self.starts: list[Path] = []

    def start(self, config_path: Path, *, foreground: bool, extra_env: dict[str, str]) -> int:
        self.starts.append(config_path)
        self._store.write(
            _record(phase=RuntimePhase.HEALTHY, pid=self._new_pid, identity=_IDENTITY_NEW)
        )
        return self._new_pid

    def force_stop(self, record: RuntimeRecord, *, grace_seconds: float = 5.0) -> bool:
        return False


def _restarter(store, control, launcher, tmp_path: Path, **kwargs):
    return SupervisorRestarter(
        control=control,
        runtime=store,
        launcher=launcher,
        config_path=tmp_path / "config.toml",
        correlation_id="c" * 24,
        sleeper=_Sleeper(),
        poll_interval_seconds=0.01,
        stop_timeout_seconds=0.2,
        start_timeout_seconds=0.2,
        **kwargs,
    )


def test_graceful_shutdown_is_recognised_even_though_the_record_survives(
    tmp_path: Path,
) -> None:
    """Review finding 2: the supervisor writes STOPPED instead of deleting the record."""
    store = _Store()
    store.write(_record(phase=RuntimePhase.HEALTHY, pid=1000, identity=_IDENTITY_OLD))
    launcher = _RecordingLauncher(store)

    outcome = _restarter(store, _Control(store), launcher, tmp_path).restart()

    assert outcome.ok is True, outcome.detail
    assert launcher.starts, "the restarter must start a replacement process"


def test_restart_requires_a_live_record_for_a_new_process(tmp_path: Path) -> None:
    """A leftover STOPPED record must not be mistaken for a started runtime."""
    store = _Store()
    store.write(_record(phase=RuntimePhase.HEALTHY, pid=1000, identity=_IDENTITY_OLD))

    class _LauncherThatNeverPublishes(_RecordingLauncher):
        def start(self, config_path: Path, *, foreground: bool, extra_env: dict[str, str]) -> int:
            self.starts.append(config_path)
            return 4242  # leaves the STOPPED record in place

    outcome = _restarter(
        store, _Control(store), _LauncherThatNeverPublishes(store), tmp_path
    ).restart()

    assert outcome.ok is False
    assert "live record" in outcome.detail


def test_restart_rejects_a_record_that_is_still_the_old_process(tmp_path: Path) -> None:
    store = _Store()
    store.write(_record(phase=RuntimePhase.HEALTHY, pid=1000, identity=_IDENTITY_OLD))

    class _LauncherReusingOldPid(_RecordingLauncher):
        def start(self, config_path: Path, *, foreground: bool, extra_env: dict[str, str]) -> int:
            self.starts.append(config_path)
            # Republish the OLD pid: this is not a new process.
            self._store.write(_record(phase=RuntimePhase.HEALTHY, pid=1000, identity=_IDENTITY_OLD))
            return 1000

    outcome = _restarter(store, _Control(store), _LauncherReusingOldPid(store), tmp_path).restart()
    assert outcome.ok is False


def test_kickstart_is_preferred_so_launchd_keeps_owning_the_supervisor(
    tmp_path: Path,
) -> None:
    """Review finding 4: never hand-spawn when the OS manager owns the job."""
    store = _Store()
    store.write(_record(phase=RuntimePhase.HEALTHY, pid=1000, identity=_IDENTITY_OLD))
    launcher = _RecordingLauncher(store)

    class _Kickstarter:
        def __init__(self) -> None:
            self.calls = 0

        def available(self) -> bool:
            return True

        def kickstart(self) -> RestartOutcome:
            self.calls += 1
            store.write(_record(phase=RuntimePhase.HEALTHY, pid=7777, identity=_IDENTITY_NEW))
            return RestartOutcome(ok=True, detail="kickstarted")

    kickstarter = _Kickstarter()
    outcome = _restarter(
        store, _Control(store), launcher, tmp_path, kickstarter=kickstarter
    ).restart()

    assert outcome.ok is True
    assert kickstarter.calls == 1
    # The manual launcher must NOT have been used.
    assert launcher.starts == []


# ------------------------------------------------- new-binary adoption (finding 1)


def test_release_aware_launcher_runs_the_stable_shim_not_the_calling_interpreter(
    tmp_path: Path,
) -> None:
    """Review finding 1: `sys.executable` is the OLD release; the shim resolves `current`."""
    shim = tmp_path / "bin" / "rf"
    shim.parent.mkdir(parents=True)
    # A shim that records its own argv so we can prove what was executed.
    marker = tmp_path / "argv.txt"
    shim.write_text(
        '#!/bin/sh\nprintf \'%s\\n\' "$0" "$@" > ' + str(marker) + "\n",
        encoding="utf-8",
    )
    shim.chmod(0o755)

    launcher = ReleaseAwareRuntimeLauncher(shim)
    code = launcher.start(tmp_path / "config.toml", foreground=True, extra_env={})

    assert code == 0
    recorded = marker.read_text(encoding="utf-8").splitlines()
    # Executed the stable shim (which resolves through `current`), not sys.executable.
    assert recorded[0] == str(shim)
    assert "start" in recorded


def test_release_aware_launcher_refuses_when_the_shim_is_missing(tmp_path: Path) -> None:
    launcher = ReleaseAwareRuntimeLauncher(tmp_path / "bin" / "rf")
    try:
        launcher.start(tmp_path / "config.toml", foreground=False, extra_env={})
    except ConfigError as exc:
        assert "LAUNCHER_SHIM_MISSING" in str(exc)
    else:  # pragma: no cover - must raise
        raise AssertionError("expected ConfigError")


# ------------------------------------------------------- observer truthfulness (F6)


def test_a_graceful_stop_really_leaves_the_record_on_disk(tmp_path: Path) -> None:
    """Pins review finding 2's premise against the REAL JsonRuntimeStore."""
    store = JsonRuntimeStore(tmp_path / "managed-runtime-v3.json")
    store.write(_record(phase=RuntimePhase.STOPPED, pid=None, identity=None))
    # The record survives a graceful stop, so "read() is None" is NOT a stop signal.
    persisted = store.read()
    assert persisted is not None
    assert persisted.phase is RuntimePhase.STOPPED


def test_observer_reports_no_release_for_a_stopped_runtime(tmp_path: Path) -> None:
    """A STOPPED record keeps `executable`; it must NOT be read as a serving release."""
    releases = tmp_path / "releases"
    executable = releases / "aaa1111" / "venv" / "bin" / "python"
    executable.parent.mkdir(parents=True)
    executable.touch()
    store = JsonRuntimeStore(tmp_path / "managed-runtime-v3.json")
    store.write(
        _record(phase=RuntimePhase.STOPPED, pid=None, identity=None, executable=str(executable))
    )

    observed = RuntimeRecordReleaseObserver(runtime=store, releases_root=releases).observe()

    assert observed.phase == "stopped"
    assert observed.running_release_sha is None


def test_observer_trusts_the_published_release_identity(tmp_path: Path) -> None:
    """Round-5 finding 1: identity is what the process published, not a derived path.

    Uses this test process's own pid/identity so the real store validates liveness.
    """
    releases = tmp_path / "releases"
    executable = releases / "aaa1111" / "venv" / "bin" / "python"
    executable.parent.mkdir(parents=True)
    executable.touch()
    live_pid = os.getpid()
    live_identity = process_identity(live_pid)
    assert live_identity is not None
    store = JsonRuntimeStore(tmp_path / "managed-runtime-v3.json")
    store.write(
        dataclasses.replace(
            _record(
                phase=RuntimePhase.HEALTHY,
                pid=live_pid,
                identity=live_identity,
                executable=str(executable),
            ),
            child_pid=live_pid,
            child_process_identity=live_identity,
            running_release_sha="aaa1111",
        )
    )

    observed = RuntimeRecordReleaseObserver(runtime=store, releases_root=releases).observe()

    assert observed.running_release_sha == "aaa1111"
    assert observed.pid == live_pid


def test_a_relocatable_venv_symlink_is_never_used_to_infer_identity(tmp_path: Path) -> None:
    """`uv venv --relocatable` makes bin/python a symlink OUT of the release tree.

    Resolving it lands in the shared uv-managed interpreter, so a path-derived identity
    silently yields nothing. A record with no published sha must report None -- never a
    guess -- and the path helper must not resolve its way out of the release directory.
    """
    releases = tmp_path / "releases"
    shared = tmp_path / "uv-python" / "bin" / "python3.13"
    shared.parent.mkdir(parents=True)
    shared.touch()
    executable = releases / "aaa1111" / "venv" / "bin" / "python"
    executable.parent.mkdir(parents=True)
    executable.symlink_to(shared)
    assert executable.resolve() == shared.resolve(), "precondition: resolves out of release"

    live_pid = os.getpid()
    live_identity = process_identity(live_pid)
    assert live_identity is not None
    store = JsonRuntimeStore(tmp_path / "managed-runtime-v3.json")
    store.write(
        dataclasses.replace(
            _record(
                phase=RuntimePhase.HEALTHY,
                pid=live_pid,
                identity=live_identity,
                executable=str(executable),
            ),
            child_pid=live_pid,
            child_process_identity=live_identity,
            running_release_sha=None,  # a legacy record: identity was never published
        )
    )
    observer = RuntimeRecordReleaseObserver(runtime=store, releases_root=releases)

    # No published identity -> no claim. The old model would have returned None here too,
    # but silently, while claiming the derived path was authoritative.
    assert observer.observe().running_release_sha is None
    # The supporting-evidence helper does NOT resolve, so it still recognises the release.
    assert observer.release_of_executable(str(executable)) == "aaa1111"


# ------------------------------- handoff: never two live supervisors (#304)


def test_the_incumbent_is_drained_before_the_os_manager_is_kickstarted(
    tmp_path: Path,
) -> None:
    """`kickstart -k` only replaces the process launchd owns.

    A supervisor started outside launchd -- a manual `rf start`, or a leftover from an
    earlier release -- survives it and keeps holding `runtime-single-instance`, so the
    incoming process dies on that lock and a healthy activation is reported as a failure.
    Draining by identity first is what makes the handoff single-instance.
    """
    events: list[str] = []
    store = _Store()
    store.write(_record(phase=RuntimePhase.HEALTHY, pid=1000, identity=_IDENTITY_OLD))

    class _RecordingControl(_Control):
        def request(self, request, *, timeout_seconds: float = 10.0):
            events.append("shutdown")
            return super().request(request, timeout_seconds=timeout_seconds)

    class _Kickstarter:
        def available(self) -> bool:
            return True

        def kickstart(self) -> RestartOutcome:
            events.append("kickstart")
            store.write(_record(phase=RuntimePhase.HEALTHY, pid=7777, identity=_IDENTITY_NEW))
            return RestartOutcome(ok=True, detail="kickstarted")

    outcome = _restarter(
        store,
        _RecordingControl(store),
        _RecordingLauncher(store),
        tmp_path,
        kickstarter=_Kickstarter(),
    ).restart()

    assert outcome.ok is True, outcome.detail
    assert events == ["shutdown", "kickstart"], "the incumbent must be gone before kickstart"


def test_an_undrainable_incumbent_is_never_kickstarted_into_a_race(tmp_path: Path) -> None:
    """If the outgoing supervisor cannot be stopped, starting a second one is worse than
    reporting the failure: that is the state the single-instance lock exists to prevent."""
    store = _Store()
    store.write(_record(phase=RuntimePhase.HEALTHY, pid=1000, identity=_IDENTITY_OLD))

    class _DeafControl:
        """SHUTDOWN is acknowledged but the process never leaves."""

        def request(self, request, *, timeout_seconds: float = 10.0):
            return ControlResponse(1, True, request.correlation_id, "ignored")

    class _Kickstarter:
        def __init__(self) -> None:
            self.calls = 0

        def available(self) -> bool:
            return True

        def kickstart(self) -> RestartOutcome:
            self.calls += 1
            return RestartOutcome(ok=True, detail="kickstarted")

    kickstarter = _Kickstarter()
    outcome = _restarter(
        store,
        _DeafControl(),
        _RecordingLauncher(store),  # force_stop() returns False: the process stays
        tmp_path,
        kickstarter=kickstarter,
    ).restart()

    assert outcome.ok is False
    assert "race" in outcome.detail
    assert kickstarter.calls == 0
