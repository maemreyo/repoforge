"""Round-3 regressions: first activation on an EMPTY release root, end to end.

These wire the production `RuntimeReleaseStore`, `UpgradeService` and
`ReleaseAwareRuntimeLauncher` together. The round-3 review found a circular dependency
the earlier per-unit tests could not see: the restarter execs
``<release-root>/bin/rf``, but that shim was only written *after* a successful restart,
so a fresh release root could never activate at all.
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
    def build(self, worktree: Path) -> BuildArtifact:
        return BuildArtifact(
            wheel_path=worktree / "wheel.whl",
            build_fingerprint=_FINGERPRINT,
            package_version="2.2.0",
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

    def restart(self) -> RestartOutcome:
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


def test_first_activation_on_an_empty_release_root_succeeds(tmp_path: Path) -> None:
    """The whole point of round-3 finding 1: bootstrap must not need a prior activation."""
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
        def restart(self) -> RestartOutcome:
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
    assert "RepoForge stable launcher" in path_launcher.read_text(encoding="utf-8")


def test_first_activation_failure_reports_that_there_is_no_rollback_target(
    tmp_path: Path,
) -> None:
    """Round-3 test 2: with no `previous`, the failure must be explicit, not silent."""
    store = RuntimeReleaseStore(tmp_path / "release-root")

    class _NeverConverges(_RealLauncherRestarter):
        def restart(self) -> RestartOutcome:
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
