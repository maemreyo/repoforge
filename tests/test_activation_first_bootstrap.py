"""Round-3/4 regressions: first activation on an EMPTY release root.

Scope caveat (round-4 finding 6): this wires the production `RuntimeReleaseStore`,
`UpgradeService` and `ReleaseAwareRuntimeLauncher`, but the installer/smoke/observer are
fakes and the installed `rf` is a stub script. It proves shim ordering, PATH-launcher
isolation and receipt durability -- it is NOT a live end-to-end activation (no real
wheel, venv, CLI, worker, socket, health probe or launchd ownership).

The round-3 review found a circular dependency the earlier per-unit tests could not
see: the restarter execs ``<release-root>/bin/rf``, but that shim was only written
*after* a successful restart, so a fresh release root could never activate at all.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from repoforge.adapters.activation.launcher import ReleaseAwareRuntimeLauncher
from repoforge.adapters.activation.release_store import RuntimeReleaseStore
from repoforge.application.activation.upgrade import UpgradeService
from repoforge.domain.errors import ConfigError
from repoforge.ports.activation import (
    BuildArtifact,
    ObservedRuntime,
    RestartOutcome,
    SmokeResult,
    WorktreeState,
)

_FINGERPRINT = "a" * 64
_SURFACE = "b" * 64
_SHA = "0123abc"


class _Inspector:
    def inspect(self, worktree: Path) -> WorktreeState:
        return WorktreeState(head_sha=_SHA, clean=True)


class _Builder:
    def build(self, worktree: Path, *, commit_sha: str) -> BuildArtifact:
        del commit_sha
        return BuildArtifact(
            wheel_path=worktree / "wheel.whl",
            build_fingerprint=_FINGERPRINT,
            package_version="2.2.0",
            source_digest="c" * 64,
        )


class _Installer:
    """Materializes a release whose `rf` is a real, runnable script."""

    def install(self, wheel: Path, destination: Path) -> None:
        binaries = destination / "venv" / "bin"
        binaries.mkdir(parents=True, exist_ok=True)
        entry = binaries / "rf"
        entry.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        entry.chmod(0o755)


class _Smoke:
    def smoke(self, release_path: Path) -> SmokeResult:
        return SmokeResult(ok=True, tool_surface_hash=_SURFACE, detail="fake")


class _Clock:
    def now_iso(self) -> str:
        return "2026-07-25T10:00:00+00:00"


class _RealLauncherRestarter:
    """Restarter that drives the PRODUCTION launcher, so a missing shim really fails."""

    def __init__(self, store: RuntimeReleaseStore, config_path: Path) -> None:
        self._store = store
        self._config_path = config_path
        self.started = 0

    def preflight_reclaim(self, departing_release: str | None = None):
        del departing_release
        return True, "", None

    def restart(
        self,
        *,
        departing_release: str | None = None,
        target_release: str | None = None,
    ) -> RestartOutcome:
        launcher = ReleaseAwareRuntimeLauncher(self._store.bin_launcher())
        try:
            launcher.start(self._config_path, foreground=True, extra_env={})
        except ConfigError as exc:
            return RestartOutcome(ok=False, detail=str(exc))
        self.started += 1
        return RestartOutcome(ok=True, detail="started via the stable shim", pid=4242)


class _ObserverFollowingCurrent:
    def __init__(self, store: RuntimeReleaseStore, restarter: _RealLauncherRestarter) -> None:
        self._store = store
        self._restarter = restarter

    def observe(self) -> ObservedRuntime:
        # Only a started runtime serves anything.
        if self._restarter.started == 0:
            return ObservedRuntime(running_release_sha=None, phase="stopped")
        return ObservedRuntime(
            running_release_sha=self._store.current_sha(), phase="healthy", pid=4242
        )


def _service(
    store: RuntimeReleaseStore, tmp_path: Path
) -> tuple[UpgradeService, _RealLauncherRestarter]:
    restarter = _RealLauncherRestarter(store, tmp_path / "config.toml")
    service = UpgradeService(
        store=store,
        inspector=_Inspector(),
        builder=_Builder(),
        installer=_Installer(),
        smoke=_Smoke(),
        restarter=restarter,
        observer=_ObserverFollowingCurrent(store, restarter),
        clock=_Clock(),
        converge_attempts=2,
        converge_interval_seconds=0,
    )
    return service, restarter


def test_first_activation_provisions_the_internal_shim_before_the_launcher_runs(
    tmp_path: Path,
) -> None:
    """Round-3 finding 1: bootstrap must not need a prior activation."""
    store = RuntimeReleaseStore(tmp_path / "release-root")
    service, restarter = _service(store, tmp_path)

    result = service.upgrade(tmp_path, activate=True)

    assert result.status == "activated"
    assert result.converged is True
    assert store.current_sha() == _SHA
    # The internal shim existed in time for the restart to use it.
    assert restarter.started == 1
    assert store.bin_launcher().is_file()
    assert os.access(store.bin_launcher(), os.X_OK)


def test_the_internal_shim_exists_before_the_restart_is_attempted(tmp_path: Path) -> None:
    """Ordering regression: assert the shim is on disk at restart time, not after."""
    store = RuntimeReleaseStore(tmp_path / "release-root")
    seen: list[bool] = []

    class _RecordingRestarter(_RealLauncherRestarter):
        def restart(
            self,
            *,
            departing_release: str | None = None,
            target_release: str | None = None,
        ) -> RestartOutcome:
            seen.append(store.bin_launcher().is_file())
            return super().restart()

    restarter = _RecordingRestarter(store, tmp_path / "config.toml")
    service = UpgradeService(
        store=store,
        inspector=_Inspector(),
        builder=_Builder(),
        installer=_Installer(),
        smoke=_Smoke(),
        restarter=restarter,
        observer=_ObserverFollowingCurrent(store, restarter),
        clock=_Clock(),
        converge_attempts=2,
        converge_interval_seconds=0,
    )
    service.upgrade(tmp_path, activate=True)

    assert seen == [True], "the internal shim must exist before the first restart"


def test_a_temporary_release_root_never_touches_the_real_path_launcher(
    tmp_path: Path,
) -> None:
    """Round-3 finding 3: `--release-root /tmp/...` must not rewrite ~/.local/bin/rf."""
    store = RuntimeReleaseStore(tmp_path / "release-root")  # no path_launcher granted
    service, _ = _service(store, tmp_path)

    service.upgrade(tmp_path, activate=True)

    assert store.path_launcher() is None
    # Nothing outside the release root was created.
    assert store.install_path_launcher() is None


def test_a_store_granted_a_path_launcher_provisions_it_after_convergence(
    tmp_path: Path,
) -> None:
    path_launcher = tmp_path / "home-bin" / "rf"
    store = RuntimeReleaseStore(tmp_path / "release-root", path_launcher=path_launcher)
    service, _ = _service(store, tmp_path)

    result = service.upgrade(tmp_path, activate=True)

    assert result.status == "activated"
    assert path_launcher.is_file()
    assert "repoforge-launcher:v1" in path_launcher.read_text(encoding="utf-8")


def test_first_activation_failure_reports_that_there_is_no_rollback_target(
    tmp_path: Path,
) -> None:
    """Round-3 test 2: with no `previous`, the failure must be explicit, not silent."""
    store = RuntimeReleaseStore(tmp_path / "release-root")

    class _NeverConverges(_RealLauncherRestarter):
        def restart(
            self,
            *,
            departing_release: str | None = None,
            target_release: str | None = None,
        ) -> RestartOutcome:
            return RestartOutcome(ok=False, detail="candidate refused to start")

    restarter = _NeverConverges(store, tmp_path / "config.toml")
    service = UpgradeService(
        store=store,
        inspector=_Inspector(),
        builder=_Builder(),
        installer=_Installer(),
        smoke=_Smoke(),
        restarter=restarter,
        observer=_ObserverFollowingCurrent(store, restarter),
        clock=_Clock(),
        converge_attempts=1,
        converge_interval_seconds=0,
    )

    with pytest.raises(ConfigError) as error:
        service.upgrade(tmp_path, activate=True)

    message = str(error.value)
    assert "ACTIVATION_FAILED" in message
    assert "No previous release exists to roll back to" in message
    # The failure is receipted so the operator can see what happened.
    outcomes = {receipt.outcome.value for receipt in store.list_receipts()}
    assert "failed" in outcomes


def test_a_path_launcher_failure_cannot_erase_a_converged_activation(
    tmp_path: Path,
) -> None:
    """Round-4 finding 1 (P0): the runtime is live; convenience must not report failure.

    An occupied/unwritable PATH launcher used to raise *after* the candidate was verified
    but *before* the receipt was written, leaving a live candidate with no durable record
    and a command that claimed failure.
    """
    occupied = tmp_path / "home-bin" / "rf"
    occupied.parent.mkdir(parents=True)
    occupied.write_text("some unrelated user script\n", encoding="utf-8")
    store = RuntimeReleaseStore(tmp_path / "release-root", path_launcher=occupied)
    service, _ = _service(store, tmp_path)

    result = service.upgrade(tmp_path, activate=True)

    # The activation is reported truthfully as a success...
    assert result.status == "activated"
    assert result.converged is True
    assert store.current_sha() == _SHA
    # ...the durable receipt exists...
    assert result.activation_receipt is not None
    receipt = store.read_receipt(result.activation_receipt)
    assert receipt is not None and receipt.outcome.value == "activated"
    # ...and the auxiliary failure is surfaced as a warning, not an exception.
    assert result.path_launcher_status == "failed"
    assert "LAUNCHER_PATH_OCCUPIED" in result.path_launcher_detail
    # The unrelated file was not clobbered.
    assert occupied.read_text(encoding="utf-8") == "some unrelated user script\n"


def test_activation_provisions_the_supervisor_shim_for_the_process_manager(
    tmp_path: Path,
) -> None:
    """Round-4 finding 2: the shim launchd execs must exist and target the worker."""
    store = RuntimeReleaseStore(tmp_path / "release-root")
    service, _ = _service(store, tmp_path)

    service.upgrade(tmp_path, activate=True)

    shim = store.supervisor_launcher()
    assert shim.is_file()
    script = shim.read_text(encoding="utf-8")
    # Execs the worker module directly, so launchd owns the supervisor pid itself.
    assert "exec " in script
    assert "repoforge.interfaces.runtime.worker" in script
    # Captures the release before exec instead of exec-ing through the mutable symlink.
    assert 'readlink "$root/current"' in script
    assert "REPOFORGE_RUNNING_RELEASE_SHA" in script
    assert "releases/$sha/venv/bin/python" in script


# ------------------------------- durable activation journal (round-4 follow-up F7)


def test_a_crash_between_the_swap_and_the_receipt_is_detectable(tmp_path: Path) -> None:
    """A moved `current` with no terminal receipt must leave evidence to reconcile."""
    store = RuntimeReleaseStore(tmp_path / "release-root")

    class _CrashingRestarter(_RealLauncherRestarter):
        def restart(
            self,
            *,
            departing_release: str | None = None,
            target_release: str | None = None,
        ) -> RestartOutcome:
            raise KeyboardInterrupt("operator killed the activation mid-flight")

    restarter = _CrashingRestarter(store, tmp_path / "config.toml")
    service = UpgradeService(
        store=store,
        inspector=_Inspector(),
        builder=_Builder(),
        installer=_Installer(),
        smoke=_Smoke(),
        restarter=restarter,
        observer=_ObserverFollowingCurrent(store, restarter),
        clock=_Clock(),
        converge_attempts=1,
        converge_interval_seconds=0,
    )

    with pytest.raises(KeyboardInterrupt):
        service.upgrade(tmp_path, activate=True)

    # `current` moved and no receipt was written...
    assert store.current_sha() == _SHA
    assert store.list_receipts() == []
    # ...but the in-flight journal records the attempt and how far it got.
    in_flight = store.read_in_flight_activation()
    assert in_flight is not None
    assert in_flight["to_sha"] == _SHA
    assert in_flight["stage"] == "symlink_switched"


def test_a_completed_activation_clears_the_journal(tmp_path: Path) -> None:
    store = RuntimeReleaseStore(tmp_path / "release-root")
    service, _ = _service(store, tmp_path)

    result = service.upgrade(tmp_path, activate=True)

    assert result.status == "activated"
    assert store.read_in_flight_activation() is None


# ------------------------ journal is forensic evidence (round-5 finding 2)


def _crash_after_swap(store: RuntimeReleaseStore, tmp_path: Path) -> None:
    class _CrashingRestarter(_RealLauncherRestarter):
        def restart(
            self,
            *,
            departing_release: str | None = None,
            target_release: str | None = None,
        ) -> RestartOutcome:
            raise KeyboardInterrupt("killed mid-activation")

    restarter = _CrashingRestarter(store, tmp_path / "config.toml")
    service = UpgradeService(
        store=store,
        inspector=_Inspector(),
        builder=_Builder(),
        installer=_Installer(),
        smoke=_Smoke(),
        restarter=restarter,
        observer=_ObserverFollowingCurrent(store, restarter),
        clock=_Clock(),
        converge_attempts=1,
        converge_interval_seconds=0,
    )
    with pytest.raises(KeyboardInterrupt):
        service.upgrade(tmp_path, activate=True)


def test_a_second_activation_is_refused_while_one_is_unterminalized(tmp_path: Path) -> None:
    """Re-running must not silently overwrite the record of the last-known-good release."""
    store = RuntimeReleaseStore(tmp_path / "release-root")
    _crash_after_swap(store, tmp_path)

    journal_before = store.journal_path().read_bytes()
    current_before = store.current_sha()
    previous_before = store.previous_sha()
    receipts_before = {r.receipt_id for r in store.list_receipts()}

    service, _ = _service(store, tmp_path)
    with pytest.raises(ConfigError, match="ACTIVATION_RECONCILIATION_REQUIRED"):
        service.upgrade(tmp_path, activate=True)

    # Forensic evidence is byte-identical and nothing moved.
    assert store.journal_path().read_bytes() == journal_before
    assert store.current_sha() == current_before
    assert store.previous_sha() == previous_before
    assert {r.receipt_id for r in store.list_receipts()} == receipts_before


def test_begin_activation_refuses_to_overwrite_an_existing_journal(tmp_path: Path) -> None:
    store = RuntimeReleaseStore(tmp_path / "release-root")
    store.begin_activation(receipt_id="act-20260725-001", from_sha="aaa1111", to_sha="bbb2222")
    before = store.journal_path().read_bytes()

    with pytest.raises(ConfigError, match="ACTIVATION_IN_FLIGHT"):
        store.begin_activation(receipt_id="act-20260725-002", from_sha="bbb2222", to_sha="ccc3333")

    assert store.journal_path().read_bytes() == before
