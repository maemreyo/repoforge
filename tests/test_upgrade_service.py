from __future__ import annotations

from pathlib import Path

import pytest

from repoforge.adapters.activation.release_store import RuntimeReleaseStore
from repoforge.application.activation.upgrade import UpgradeService
from repoforge.domain.errors import ConfigError
from repoforge.ports.activation import (
    BuildArtifact,
    HealthSample,
    ObservedRuntime,
    RestartOutcome,
    SmokeResult,
    WorktreeState,
)

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


class _Restarter:
    """Restarter fake; `serving` is what the runtime runs after each restart."""

    def __init__(self, *, ok: bool = True, serving: list[str] | None = None) -> None:
        self._ok = ok
        self.calls = 0
        self.serving = serving

    def restart(self) -> RestartOutcome:
        self.calls += 1
        return RestartOutcome(ok=self._ok, detail="fake restart", pid=99 if self._ok else None)


class _Observer:
    """Observes whatever the fake release store's `current` points at (converged),
    unless pinned to a specific sha to simulate a runtime that did not adopt."""

    def __init__(self, store, *, pinned: str | None = None, phase: str = "healthy") -> None:
        self._store = store
        self._pinned = pinned
        self._phase = phase

    def observe(self) -> ObservedRuntime:
        sha = self._pinned if self._pinned is not None else self._store.current_sha()
        return ObservedRuntime(running_release_sha=sha, phase=self._phase, pid=99)


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
    restarter: _Restarter | None = None,
    head: str = _CLEAN_SHA,
    store: RuntimeReleaseStore | None = None,
    pinned_observed: str | None = None,
) -> tuple[UpgradeService, RuntimeReleaseStore, _Restarter]:
    used_store = store or RuntimeReleaseStore(tmp_path / "release-root")
    used_restarter = restarter or _Restarter()
    service = UpgradeService(
        store=used_store,
        inspector=_Inspector(WorktreeState(head_sha=head, clean=clean, dirty_detail="M f.py")),
        builder=_Builder(),
        installer=_Installer(),
        smoke=_Smoke(ok=smoke_ok, surface=surface),
        restarter=used_restarter,
        observer=_Observer(used_store, pinned=pinned_observed),
        clock=_Clock(),
        converge_attempts=2,
        converge_interval_seconds=0,
    )
    return service, used_store, used_restarter


def test_dirty_worktree_is_refused_before_building(tmp_path: Path) -> None:
    service, _, _ = _service(tmp_path, clean=False)
    with pytest.raises(ConfigError, match="WORKTREE_DIRTY"):
        service.upgrade(tmp_path, activate=True)


def test_failed_smoke_test_aborts_before_activation(tmp_path: Path) -> None:
    service, store, restarter = _service(tmp_path, smoke_ok=False)
    with pytest.raises(ConfigError, match="SMOKE_FAILED"):
        service.upgrade(tmp_path, activate=True)
    assert store.current_sha() is None
    assert restarter.calls == 0


def test_staged_upgrade_installs_without_switching_current(tmp_path: Path) -> None:
    service, store, restarter = _service(tmp_path)
    result = service.upgrade(tmp_path, activate=False)
    assert result.status == "staged"
    assert store.read_manifest(_CLEAN_SHA) is not None
    assert store.current_sha() is None
    assert restarter.calls == 0


def test_activate_swaps_current_writes_receipt_and_reloads(tmp_path: Path) -> None:
    service, store, restarter = _service(tmp_path)
    result = service.upgrade(tmp_path, activate=True)

    assert result.status == "activated"
    assert store.current_sha() == _CLEAN_SHA
    assert result.activation_receipt is not None
    assert store.read_receipt(result.activation_receipt) is not None
    assert restarter.calls == 1
    assert (
        result.as_dict()["rollback_command"] == f"rf upgrade rollback {result.activation_receipt}"
    )
    # First activation from nothing: no prior surface to compare, no rediscovery.
    assert result.rediscovery_required is False


def test_activation_flags_rediscovery_when_the_tool_surface_changes(tmp_path: Path) -> None:
    first, store, _ = _service(tmp_path, surface=_SURFACE, head="1111aaa")
    first.upgrade(tmp_path, activate=True)
    second, _, _ = _service(tmp_path, surface=_SURFACE_NEW, head="2222bbb", store=store)
    result = second.upgrade(tmp_path, activate=True)
    assert result.rediscovery_required is True
    assert store.previous_sha() == "1111aaa"


def test_rollback_returns_to_the_previous_release(tmp_path: Path) -> None:
    first, store, _ = _service(tmp_path, head="1111aaa")
    first.upgrade(tmp_path, activate=True)
    second, _, _ = _service(tmp_path, head="2222bbb", store=store)
    second.upgrade(tmp_path, activate=True)
    assert store.current_sha() == "2222bbb"

    result = second.rollback()
    assert result.status == "rolled_back"
    assert store.current_sha() == "1111aaa"
    assert result.rediscovery_required is True


# ------------------------------------------- truthful activation (review findings 1/3)


def test_activation_requires_the_runtime_to_actually_serve_the_candidate(
    tmp_path: Path,
) -> None:
    """A restart that leaves the old release serving must NOT report success."""
    first, store, _ = _service(tmp_path, head="1111aaa")
    first.upgrade(tmp_path, activate=True)

    # The runtime stays pinned on the old release: convergence can never be observed.
    second, _, _ = _service(tmp_path, head="2222bbb", store=store, pinned_observed="1111aaa")
    with pytest.raises(ConfigError, match="ACTIVATION_FAILED_ROLLED_BACK"):
        second.upgrade(tmp_path, activate=True)

    # Rolled back to the release that is actually serving, and both receipts exist.
    assert store.current_sha() == "1111aaa"
    outcomes = {r.outcome.value for r in store.list_receipts()}
    assert "failed" in outcomes
    assert "rolled_back" in outcomes


def test_a_failed_restart_never_soft_succeeds(tmp_path: Path) -> None:
    first, store, _ = _service(tmp_path, head="1111aaa")
    first.upgrade(tmp_path, activate=True)

    second, _, _ = _service(tmp_path, head="2222bbb", store=store, restarter=_Restarter(ok=False))
    with pytest.raises(ConfigError, match="ACTIVATION_FAILED"):
        second.upgrade(tmp_path, activate=True)
    assert store.current_sha() == "1111aaa"


def test_activated_receipt_records_the_observed_release_and_convergence(
    tmp_path: Path,
) -> None:
    service, store, _ = _service(tmp_path)
    result = service.upgrade(tmp_path, activate=True)
    assert result.converged is True
    assert result.observed_sha == _CLEAN_SHA
    assert result.stage == "health_verified"
    receipt = store.read_receipt(result.activation_receipt or "")
    assert receipt is not None
    assert receipt.converged is True
    assert receipt.observed_sha == _CLEAN_SHA


# --------------------------------------------- receipted rollback (review finding 4)


def test_rollback_targets_the_release_named_by_the_receipt(tmp_path: Path) -> None:
    first, store, _ = _service(tmp_path, head="1111aaa")
    first.upgrade(tmp_path, activate=True)
    second, _, _ = _service(tmp_path, head="2222bbb", store=store)
    activated = second.upgrade(tmp_path, activate=True)

    rolled = second.rollback(activated.activation_receipt)
    assert rolled.active_sha == "1111aaa"
    receipt = store.read_receipt(rolled.activation_receipt or "")
    assert receipt is not None
    # Structured linkage, not a detail string.
    assert receipt.cause_receipt_id == activated.activation_receipt


def test_repeating_a_receipted_rollback_is_refused_not_toggled(tmp_path: Path) -> None:
    first, store, _ = _service(tmp_path, head="1111aaa")
    first.upgrade(tmp_path, activate=True)
    second, _, _ = _service(tmp_path, head="2222bbb", store=store)
    activated = second.upgrade(tmp_path, activate=True)
    second.rollback(activated.activation_receipt)
    assert store.current_sha() == "1111aaa"

    # Re-running the same receipted rollback must not toggle forward again.
    with pytest.raises(ConfigError, match="ROLLBACK_STATE_MISMATCH"):
        second.rollback(activated.activation_receipt)
    assert store.current_sha() == "1111aaa"


def test_rollback_refuses_an_unknown_receipt(tmp_path: Path) -> None:
    service, _, _ = _service(tmp_path)
    with pytest.raises(ConfigError, match="RECEIPT_NOT_FOUND"):
        service.rollback("act-20260725-999")


# ------------------------------------------ immutable releases (review finding 7)


def test_reinstalling_the_same_commit_with_different_bits_is_refused(
    tmp_path: Path,
) -> None:
    service, store, _ = _service(tmp_path)
    service.upgrade(tmp_path, activate=False)

    # Same commit sha, different wheel fingerprint -> refuse rather than overwrite.
    conflicting = UpgradeService(
        store=store,
        inspector=_Inspector(WorktreeState(head_sha=_CLEAN_SHA, clean=True)),
        builder=_Builder(fingerprint="d" * 64),
        installer=_Installer(),
        smoke=_Smoke(),
        restarter=_Restarter(),
        observer=_Observer(store),
        clock=_Clock(),
    )
    with pytest.raises(ConfigError, match="RELEASE_FINGERPRINT_CONFLICT"):
        conflicting.upgrade(tmp_path, activate=False)


def test_receipts_are_immutable(tmp_path: Path) -> None:
    from repoforge.domain.activation import ActivationOutcome, ActivationReceipt

    store = RuntimeReleaseStore(tmp_path / "release-root")
    receipt = ActivationReceipt(
        receipt_id="act-20260725-001",
        from_sha=None,
        to_sha="aaa1111",
        to_fingerprint=_FINGERPRINT,
        tool_surface_hash=_SURFACE,
        rediscovery_required=False,
        outcome=ActivationOutcome.ROLLED_BACK,
        activated_at="2026-07-25T10:00:00+00:00",
    )
    store.write_receipt(receipt)
    with pytest.raises(ConfigError, match="RECEIPT_EXISTS"):
        store.write_receipt(receipt)


def _watched_service(
    tmp_path: Path,
    *,
    store: RuntimeReleaseStore,
    samples: list[HealthSample],
    head: str,
    threshold: int = 1,
) -> tuple[UpgradeService, _HealthProbe, _Sleeper]:
    probe = _HealthProbe(samples)
    sleeper = _Sleeper()
    service = UpgradeService(
        store=store,
        inspector=_Inspector(WorktreeState(head_sha=head, clean=True)),
        builder=_Builder(),
        installer=_Installer(),
        smoke=_Smoke(ok=True, surface=_SURFACE),
        restarter=_Restarter(),
        observer=_Observer(store),
        clock=_Clock(),
        health_probe=probe,
        sleeper=sleeper,
        converge_attempts=2,
        converge_interval_seconds=0,
        health_failure_threshold=threshold,
    )
    return service, probe, sleeper


def test_watch_keeps_the_candidate_active_when_health_holds(tmp_path: Path) -> None:
    first, store, _ = _service(tmp_path, head="1111aaa")
    first.upgrade(tmp_path, activate=True)

    service, _, _ = _watched_service(
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


def test_watch_auto_rolls_back_when_the_candidate_degrades(tmp_path: Path) -> None:
    first, store, _ = _service(tmp_path, head="1111aaa")
    first.upgrade(tmp_path, activate=True)

    service, _, _ = _watched_service(
        tmp_path,
        store=store,
        # healthy for the activation gate, then degrades inside the window
        samples=[
            HealthSample(healthy=True, detail="ok at activation"),
            HealthSample(healthy=False, detail="crash-loop"),
        ],
        head="2222bbb",
    )
    result = service.upgrade(
        tmp_path, activate=True, watch=True, health_window_seconds=10, health_interval_seconds=5
    )

    assert result.status == "rolled_back"
    assert store.current_sha() == "1111aaa"
    assert "Auto-rollback" in result.detail
    assert "crash-loop" in result.detail


def test_a_transient_blip_does_not_trigger_rollback(tmp_path: Path) -> None:
    """One unhealthy sample inside the window must not roll a good release back."""
    first, store, _ = _service(tmp_path, head="1111aaa")
    first.upgrade(tmp_path, activate=True)

    service, _, _ = _watched_service(
        tmp_path,
        store=store,
        # blip, then healthy for the rest of the window
        samples=[
            HealthSample(healthy=True, detail="ok at activation"),
            HealthSample(healthy=False, detail="one slow probe"),
            HealthSample(healthy=True, detail="ok"),
        ],
        head="2222bbb",
        threshold=3,
    )
    result = service.upgrade(
        tmp_path, activate=True, watch=True, health_window_seconds=50, health_interval_seconds=5
    )

    assert result.status == "activated"
    assert store.current_sha() == "2222bbb"


def test_sustained_failure_crosses_the_threshold_and_rolls_back(tmp_path: Path) -> None:
    first, store, _ = _service(tmp_path, head="1111aaa")
    first.upgrade(tmp_path, activate=True)

    service, _, _ = _watched_service(
        tmp_path,
        store=store,
        samples=[
            HealthSample(healthy=True, detail="ok at activation"),
            HealthSample(healthy=False, detail="crash-loop"),
        ],
        head="2222bbb",
        threshold=3,
    )
    result = service.upgrade(
        tmp_path, activate=True, watch=True, health_window_seconds=50, health_interval_seconds=5
    )
    assert result.status == "rolled_back"
    assert "3 consecutive unhealthy samples" in result.detail


def test_watch_without_a_probe_is_a_no_op(tmp_path: Path) -> None:
    service, store, _ = _service(tmp_path, head="2222bbb")
    result = service.upgrade(tmp_path, activate=True, watch=True)
    assert result.status == "activated"
    assert store.current_sha() == "2222bbb"
