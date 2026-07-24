from __future__ import annotations

from pathlib import Path

import pytest

from repoforge.adapters.activation.release_store import RuntimeReleaseStore
from repoforge.application.activation.upgrade import UpgradeService
from repoforge.domain.errors import ConfigError
from repoforge.ports.activation import BuildArtifact, HealthSample, SmokeResult, WorktreeState

_FINGERPRINT = "a" * 64
_SURFACE = "b" * 64
_SURFACE_NEW = "c" * 64
_CLEAN_SHA = "0123abc"


class _Inspector:
    def __init__(self, state: WorktreeState) -> None:
        self._state = state

    def inspect(self, worktree: Path) -> WorktreeState:
        return self._state


class _Builder:
    def __init__(self, fingerprint: str = _FINGERPRINT) -> None:
        self._fingerprint = fingerprint

    def build(self, worktree: Path) -> BuildArtifact:
        return BuildArtifact(
            wheel_path=worktree / "dist" / "wheel.whl",
            build_fingerprint=self._fingerprint,
            package_version="2.2.0",
        )


class _Installer:
    def install(self, wheel: Path, destination: Path) -> None:
        (destination / "venv" / "bin").mkdir(parents=True, exist_ok=True)


class _Smoke:
    def __init__(self, *, ok: bool = True, surface: str = _SURFACE) -> None:
        self._ok = ok
        self._surface = surface

    def smoke(self, release_path: Path) -> SmokeResult:
        return SmokeResult(ok=self._ok, tool_surface_hash=self._surface, detail="fake")


class _Reloader:
    def __init__(self, *, ok: bool = True) -> None:
        self._ok = ok
        self.calls = 0

    def reload(self) -> bool:
        self.calls += 1
        return self._ok


class _Clock:
    def now_iso(self) -> str:
        return "2026-07-25T10:00:00+00:00"


class _Sleeper:
    def __init__(self) -> None:
        self.slept: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)


class _HealthProbe:
    """Return scripted samples; the last one repeats once exhausted."""

    def __init__(self, samples: list[HealthSample]) -> None:
        self._samples = samples
        self.calls = 0

    def sample(self) -> HealthSample:
        index = min(self.calls, len(self._samples) - 1)
        self.calls += 1
        return self._samples[index]


def _service(
    tmp_path: Path,
    *,
    clean: bool = True,
    smoke_ok: bool = True,
    surface: str = _SURFACE,
    reloader: _Reloader | None = None,
    head: str = _CLEAN_SHA,
) -> tuple[UpgradeService, RuntimeReleaseStore, _Reloader]:
    store = RuntimeReleaseStore(tmp_path / "release-root")
    used_reloader = reloader or _Reloader()
    service = UpgradeService(
        store=store,
        inspector=_Inspector(WorktreeState(head_sha=head, clean=clean, dirty_detail="M f.py")),
        builder=_Builder(),
        installer=_Installer(),
        smoke=_Smoke(ok=smoke_ok, surface=surface),
        reloader=used_reloader,
        clock=_Clock(),
    )
    return service, store, used_reloader


def test_dirty_worktree_is_refused_before_building(tmp_path: Path) -> None:
    service, _, _ = _service(tmp_path, clean=False)
    with pytest.raises(ConfigError, match="WORKTREE_DIRTY"):
        service.upgrade(tmp_path, activate=True)


def test_failed_smoke_test_aborts_before_activation(tmp_path: Path) -> None:
    service, store, reloader = _service(tmp_path, smoke_ok=False)
    with pytest.raises(ConfigError, match="SMOKE_FAILED"):
        service.upgrade(tmp_path, activate=True)
    assert store.current_sha() is None
    assert reloader.calls == 0


def test_staged_upgrade_installs_without_switching_current(tmp_path: Path) -> None:
    service, store, reloader = _service(tmp_path)
    result = service.upgrade(tmp_path, activate=False)
    assert result.status == "staged"
    assert store.read_manifest(_CLEAN_SHA) is not None
    assert store.current_sha() is None
    assert reloader.calls == 0


def test_activate_swaps_current_writes_receipt_and_reloads(tmp_path: Path) -> None:
    service, store, reloader = _service(tmp_path)
    result = service.upgrade(tmp_path, activate=True)

    assert result.status == "activated"
    assert store.current_sha() == _CLEAN_SHA
    assert result.activation_receipt is not None
    assert store.read_receipt(result.activation_receipt) is not None
    assert reloader.calls == 1
    assert (
        result.as_dict()["rollback_command"] == f"rf upgrade rollback {result.activation_receipt}"
    )
    # First activation from nothing: no prior surface to compare, no rediscovery.
    assert result.rediscovery_required is False


def test_activation_flags_rediscovery_when_the_tool_surface_changes(tmp_path: Path) -> None:
    reloader = _Reloader()
    # First release establishes a surface.
    first, store, _ = _service(tmp_path, surface=_SURFACE, reloader=reloader, head="1111aaa")
    first.upgrade(tmp_path, activate=True)
    # Second release with a different surface, same store.
    second = UpgradeService(
        store=store,
        inspector=_Inspector(WorktreeState(head_sha="2222bbb", clean=True)),
        builder=_Builder(),
        installer=_Installer(),
        smoke=_Smoke(ok=True, surface=_SURFACE_NEW),
        reloader=reloader,
        clock=_Clock(),
    )
    result = second.upgrade(tmp_path, activate=True)
    assert result.rediscovery_required is True
    assert store.previous_sha() == "1111aaa"


def test_rollback_returns_to_the_previous_release(tmp_path: Path) -> None:
    reloader = _Reloader()
    first, store, _ = _service(tmp_path, reloader=reloader, head="1111aaa")
    first.upgrade(tmp_path, activate=True)
    second = UpgradeService(
        store=store,
        inspector=_Inspector(WorktreeState(head_sha="2222bbb", clean=True)),
        builder=_Builder(),
        installer=_Installer(),
        smoke=_Smoke(ok=True, surface=_SURFACE),
        reloader=reloader,
        clock=_Clock(),
    )
    second.upgrade(tmp_path, activate=True)
    assert store.current_sha() == "2222bbb"

    result = second.rollback()
    assert result.status == "rolled_back"
    assert store.current_sha() == "1111aaa"
    assert result.rediscovery_required is True


def _watched_service(
    tmp_path: Path,
    *,
    store: RuntimeReleaseStore,
    samples: list[HealthSample],
    head: str,
) -> tuple[UpgradeService, _HealthProbe, _Sleeper]:
    probe = _HealthProbe(samples)
    sleeper = _Sleeper()
    service = UpgradeService(
        store=store,
        inspector=_Inspector(WorktreeState(head_sha=head, clean=True)),
        builder=_Builder(),
        installer=_Installer(),
        smoke=_Smoke(ok=True, surface=_SURFACE),
        reloader=_Reloader(),
        clock=_Clock(),
        health_probe=probe,
        sleeper=sleeper,
    )
    return service, probe, sleeper


def test_watch_keeps_the_candidate_active_when_health_holds(tmp_path: Path) -> None:
    store = RuntimeReleaseStore(tmp_path / "release-root")
    # Establish a warm previous so an (unwanted) rollback would be possible.
    first, _, _ = _service(tmp_path, head="1111aaa")
    first.upgrade(tmp_path, activate=True)

    service, probe, sleeper = _watched_service(
        tmp_path,
        store=store,
        samples=[HealthSample(healthy=True, detail="ok")],
        head="2222bbb",
    )
    result = service.upgrade(
        tmp_path, activate=True, watch=True, health_window_seconds=10, health_interval_seconds=5
    )

    assert result.status == "activated"
    assert store.current_sha() == "2222bbb"
    assert probe.calls == 2  # window // interval = 2 samples
    assert sleeper.slept == [5, 5]


def test_watch_auto_rolls_back_when_the_candidate_degrades(tmp_path: Path) -> None:
    store = RuntimeReleaseStore(tmp_path / "release-root")
    first, _, _ = _service(tmp_path, head="1111aaa")
    first.upgrade(tmp_path, activate=True)

    service, _, _ = _watched_service(
        tmp_path,
        store=store,
        samples=[HealthSample(healthy=False, detail="crash-loop")],
        head="2222bbb",
    )
    result = service.upgrade(
        tmp_path, activate=True, watch=True, health_window_seconds=10, health_interval_seconds=5
    )

    assert result.status == "rolled_back"
    # Warm previous is restored automatically.
    assert store.current_sha() == "1111aaa"
    assert "Auto-rollback" in result.detail
    assert "crash-loop" in result.detail


def test_watch_without_a_probe_is_a_no_op(tmp_path: Path) -> None:
    # A service built without a probe must not block activation on a health window.
    service, store, _ = _service(tmp_path, head="2222bbb")
    result = service.upgrade(tmp_path, activate=True, watch=True)
    assert result.status == "activated"
    assert store.current_sha() == "2222bbb"
