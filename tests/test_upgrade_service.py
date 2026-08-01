from __future__ import annotations

from dataclasses import replace
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
    def __init__(
        self,
        *,
        ok: bool = True,
        surface: str = _SURFACE,
        contract_identity: str = "",
        mismatched_fields: tuple[str, ...] = (),
        artifact_paths: tuple[str, ...] = (),
    ) -> None:
        self._ok = ok
        self._surface = surface
        self._contract_identity = contract_identity
        self._mismatched = mismatched_fields
        self._paths = artifact_paths

    def smoke(self, release_path: Path) -> SmokeResult:
        return SmokeResult(
            ok=self._ok,
            tool_surface_hash=self._surface,
            detail="fake",
            contract_identity=self._contract_identity,
            contract_mismatched_fields=self._mismatched,
            contract_artifact_paths=self._paths,
        )


class _Restarter:
    """Restarter fake; `serving` is what the runtime runs after each restart."""

    def __init__(
        self,
        *,
        ok: bool = True,
        serving: list[str] | None = None,
        reclamation: dict[str, object] | None = None,
    ) -> None:
        self._ok = ok
        self.calls = 0
        self.serving = serving
        self.departing_releases: list[str | None] = []
        self._reclamation = reclamation

    def restart(self, *, departing_release: str | None = None) -> RestartOutcome:
        self.calls += 1
        self.departing_releases.append(departing_release)
        return RestartOutcome(
            ok=self._ok,
            detail="fake restart",
            pid=99 if self._ok else None,
            reclamation=self._reclamation,
        )


class _Observer:
    """Observes whatever the fake release store's `current` points at (converged),
    unless pinned to a specific sha to simulate a runtime that did not adopt."""

    def __init__(
        self,
        store,
        *,
        pinned: str | None = None,
        phase: str = "healthy",
        last_error_code: str | None = None,
        fail_closed_since: str | None = None,
    ) -> None:
        self._store = store
        self._pinned = pinned
        self._phase = phase
        self._last_error_code = last_error_code
        self._fail_closed_since = fail_closed_since

    def observe(self) -> ObservedRuntime:
        sha = self._pinned if self._pinned is not None else self._store.current_sha()
        return ObservedRuntime(
            running_release_sha=sha,
            phase=self._phase,
            pid=99,
            last_error_code=self._last_error_code,
            fail_closed_since=self._fail_closed_since,
        )


class _Clock:
    def now_iso(self) -> str:
        return "2026-07-25T10:00:00+00:00"


class _Sleeper:
    def __init__(self) -> None:
        self.slept: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)


class _PhaseTrackingObserver:
    """Reports fail_closed while `current` is the broken release, healthy after rollback."""

    def __init__(
        self,
        store,
        *,
        failed_sha: str,
        error_code: str,
        fail_closed_since: str | None = "2026-07-25T10:06:00+00:00",
    ) -> None:
        self._store = store
        self._failed_sha = failed_sha
        self._error_code = error_code
        self._fail_closed_since = fail_closed_since

    def observe(self) -> ObservedRuntime:
        sha = self._store.current_sha()
        failed = sha == self._failed_sha
        return ObservedRuntime(
            running_release_sha=sha,
            phase="fail_closed" if failed else "healthy",
            pid=99,
            last_error_code=self._error_code if failed else None,
            fail_closed_since=self._fail_closed_since if failed else None,
        )


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
    contract_identity: str = "",
    smoke_mismatch: tuple[str, ...] = (),
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
        smoke=_Smoke(
            ok=smoke_ok,
            surface=surface,
            contract_identity=contract_identity,
            mismatched_fields=smoke_mismatch,
        ),
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


def test_contract_identity_mismatch_blocks_activation_before_any_swap(
    tmp_path: Path,
) -> None:
    """A divergent candidate is refused before the journal opens or `current` moves."""
    service, store, restarter = _service(
        tmp_path, smoke_mismatch=("input_contract_digest",), contract_identity=""
    )
    with pytest.raises(ConfigError, match="RELEASE_CONTRACT_IDENTITY_MISMATCH"):
        service.upgrade(tmp_path, activate=True)
    assert store.current_sha() is None
    assert restarter.calls == 0
    assert store.read_in_flight_activation() is None


def test_contract_identity_drift_on_an_existing_release_is_refused(tmp_path: Path) -> None:
    """An installed release whose contract identity drifted from its manifest is refused."""
    first, store, _ = _service(tmp_path, contract_identity="d" * 64, head="1111aaa")
    first.upgrade(tmp_path, activate=True)
    assert store.read_manifest("1111aaa") is not None

    second, _, restarter = _service(
        tmp_path, contract_identity="e" * 64, head="1111aaa", store=store
    )
    with pytest.raises(ConfigError, match="RELEASE_CONTRACT_IDENTITY_MISMATCH"):
        second.upgrade(tmp_path, activate=True)
    # `current` already points at 1111aaa from the first activation, so the meaningful
    # assertion is that the drift was detected without any restart.
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


def test_activation_passes_the_departing_release_to_the_restarter(tmp_path: Path) -> None:
    """Orphan reclamation must know which release is departing (#368)."""
    first, store, restarter = _service(tmp_path, head="1111aaa")
    first.upgrade(tmp_path, activate=True)
    assert restarter.departing_releases == [None]

    second, _, restarter = _service(tmp_path, head="2222bbb", store=store)
    second.upgrade(tmp_path, activate=True)
    assert restarter.departing_releases == ["1111aaa"]


def test_rollback_passes_the_current_release_as_departing(tmp_path: Path) -> None:
    first, store, _ = _service(tmp_path, head="1111aaa")
    first.upgrade(tmp_path, activate=True)
    second, _, restarter = _service(tmp_path, head="2222bbb", store=store)
    second.upgrade(tmp_path, activate=True)

    second.rollback()

    assert restarter.departing_releases[-1] == "2222bbb"


def test_activation_receipt_records_worker_reclamation(tmp_path: Path) -> None:
    """The receipt carries the reclamation evidence so a rollback can be audited (#368)."""
    reclamation = {
        "inspected": 2,
        "reclaimed": 1,
        "already_gone": 1,
        "refused_unproven": 0,
        "survived_kill": 0,
        "worker_ids": ["worker-0123456789ab"],
        "pids": [4242],
        "release_shas": ["1111aaa"],
        "detail": "reconciled 2 execution worker binding(s)",
    }
    service, store, _ = _service(tmp_path, restarter=_Restarter(reclamation=reclamation))
    result = service.upgrade(tmp_path, activate=True)

    receipt = store.read_receipt(result.activation_receipt)
    assert receipt is not None
    assert receipt.worker_reclamation == reclamation


def test_survived_worker_failure_is_reported_not_swallowed(tmp_path: Path) -> None:
    """A worker that survived SIGKILL must block the activation, not quietly proceed."""
    service, _, _ = _service(
        tmp_path,
        restarter=_Restarter(
            ok=False,
            reclamation={
                "inspected": 1,
                "reclaimed": 0,
                "already_gone": 0,
                "refused_unproven": 0,
                "survived_kill": 1,
                "worker_ids": [],
                "pids": [],
                "release_shas": [],
                "detail": "worker survived SIGKILL",
            },
        ),
    )
    with pytest.raises(ConfigError, match="ACTIVATION_FAILED"):
        service.upgrade(tmp_path, activate=True)


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
    from repoforge.domain.activation import (
        ActivationOutcome,
        ActivationReceipt,
        ActivationStage,
    )

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
        stage=ActivationStage.HEALTH_VERIFIED,
        observed_sha="aaa1111",
        converged=True,
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
            # Rolling back to the known-good release recovers, so the rollback itself
            # converges and may truthfully report success.
            HealthSample(healthy=True, detail="previous release healthy"),
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
            HealthSample(healthy=False, detail="crash-loop"),
            HealthSample(healthy=False, detail="crash-loop"),
            HealthSample(healthy=True, detail="previous release healthy"),
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


# ------------------------------------------ rollback fail-closed (round-2 finding 3)


def test_rollback_that_does_not_converge_is_not_reported_as_rolled_back(
    tmp_path: Path,
) -> None:
    """A symlink swap is not a rollback: an unconverged rollback must not claim success."""
    first, store, _ = _service(tmp_path, head="1111aaa")
    first.upgrade(tmp_path, activate=True)
    second, _, _ = _service(tmp_path, head="2222bbb", store=store)
    activated = second.upgrade(tmp_path, activate=True)

    # Now roll back with a restarter that fails: the runtime cannot adopt the target.
    failing = UpgradeService(
        store=store,
        inspector=_Inspector(WorktreeState(head_sha="2222bbb", clean=True)),
        builder=_Builder(),
        installer=_Installer(),
        smoke=_Smoke(),
        restarter=_Restarter(ok=False),
        # The runtime is still serving the candidate, so rolling back to 1111aaa can
        # never be observed as converged.
        observer=_Observer(store, pinned="2222bbb"),
        clock=_Clock(),
        converge_attempts=1,
        converge_interval_seconds=0,
    )
    result = failing.rollback(activated.activation_receipt)

    assert result.status == "rollback_failed"
    assert result.converged is False
    assert result.active_sha is None
    receipt = store.read_receipt(result.activation_receipt or "")
    assert receipt is not None
    assert receipt.outcome.value == "rollback_failed"


def test_rollback_validates_the_target_before_moving_current(tmp_path: Path) -> None:
    """Round-2 finding 12: a receipt naming a pruned release must not move `current`."""
    import shutil

    first, store, _ = _service(tmp_path, head="1111aaa")
    first.upgrade(tmp_path, activate=True)
    second, _, _ = _service(tmp_path, head="2222bbb", store=store)
    activated = second.upgrade(tmp_path, activate=True)

    # Destroy the rollback target's release directory.
    shutil.rmtree(store.release_path("1111aaa"))

    with pytest.raises(ConfigError, match="ROLLBACK_TARGET_UNUSABLE"):
        second.rollback(activated.activation_receipt)
    # `current` is untouched because validation happened first.
    assert store.current_sha() == "2222bbb"


def test_concurrent_activations_are_serialized_by_the_activation_lock(
    tmp_path: Path,
) -> None:
    """Round-2 finding 8: the pipeline must hold one lock across swap/restart/receipt."""
    calls: list[str] = []

    class _Locks:
        def lock(self, name, *, timeout_seconds=None, metadata=None):
            from contextlib import contextmanager

            @contextmanager
            def _held():
                calls.append(name)
                yield

            return _held()

        def path_for(self, name):
            return tmp_path / name

    store = RuntimeReleaseStore(tmp_path / "release-root")
    service = UpgradeService(
        store=store,
        inspector=_Inspector(WorktreeState(head_sha=_CLEAN_SHA, clean=True)),
        builder=_Builder(),
        installer=_Installer(),
        smoke=_Smoke(),
        restarter=_Restarter(),
        observer=_Observer(store),
        clock=_Clock(),
        converge_attempts=1,
        converge_interval_seconds=0,
        locks=_Locks(),
    )
    service.upgrade(tmp_path, activate=True)
    # Exactly one lock acquisition wraps the whole activation (no nested re-entry).
    assert calls == ["runtime-activation"]


# ------------------- switching to an already-installed release (branch/short sha)


def test_switch_activates_an_installed_release_by_branch_name(tmp_path: Path) -> None:
    """Switching branches must not require re-running the build against a worktree.

    The release is already installed and immutable, so `switch` takes the same
    journal -> swap -> restart -> observe -> receipt path a fresh activation does.
    """
    first, store, _ = _service(tmp_path, head="1111aaa")
    first.upgrade(tmp_path, activate=True)
    second, _, restarter = _service(tmp_path, head="2222bbb", store=store)
    second.upgrade(tmp_path, activate=True)
    assert store.current_sha() == "2222bbb"
    # Label the installed releases the way a real build would.
    for sha, branch in (("1111aaa", "main"), ("2222bbb", "feat/x")):
        existing = store.read_manifest(sha)
        assert existing is not None
        store.write_manifest(replace(existing, branch=branch))
    restarts_before = restarter.calls

    result = second.switch("main")

    assert result.status == "activated"
    assert result.converged is True
    assert result.active_sha == "1111aaa"
    assert store.current_sha() == "1111aaa"
    assert store.previous_sha() == "2222bbb"
    # A switch is an activation: it restarts the runtime and writes its own receipt.
    assert restarter.calls == restarts_before + 1
    assert result.activation_receipt is not None
    receipt = store.read_receipt(result.activation_receipt)
    assert receipt is not None
    assert receipt.to_sha == "1111aaa"
    assert receipt.converged is True


def test_switch_accepts_a_short_sha(tmp_path: Path) -> None:
    first, store, _ = _service(tmp_path, head="1111aaa")
    first.upgrade(tmp_path, activate=True)
    second, _, _ = _service(tmp_path, head="2222bbb", store=store)
    second.upgrade(tmp_path, activate=True)

    assert second.switch("1111").active_sha == "1111aaa"


def test_switching_to_the_active_release_does_not_restart_anything(tmp_path: Path) -> None:
    """The operator asked for a state that already holds; restarting a healthy runtime
    for nothing would be a worse answer than saying so."""
    service, store, restarter = _service(tmp_path, head="1111aaa")
    service.upgrade(tmp_path, activate=True)
    restarts_before = restarter.calls

    result = service.switch("1111aaa")

    assert result.status == "already_active"
    assert result.converged is True
    assert restarter.calls == restarts_before
    assert store.current_sha() == "1111aaa"


def test_switch_refuses_an_unknown_release(tmp_path: Path) -> None:
    service, store, _ = _service(tmp_path, head="1111aaa")
    service.upgrade(tmp_path, activate=True)

    with pytest.raises(ConfigError, match="RELEASE_NOT_FOUND"):
        service.switch("no-such-branch")
    assert store.current_sha() == "1111aaa"


def test_switch_refuses_while_an_activation_is_unterminalized(tmp_path: Path) -> None:
    """An unreconciled activation means the on-disk state is not trustworthy yet."""
    first, store, _ = _service(tmp_path, head="1111aaa")
    first.upgrade(tmp_path, activate=True)
    second, _, _ = _service(tmp_path, head="2222bbb", store=store)
    second.upgrade(tmp_path, activate=True)
    store.begin_activation(receipt_id="act-20260725-900", from_sha="2222bbb", to_sha="1111aaa")

    with pytest.raises(ConfigError, match="ACTIVATION_RECONCILIATION_REQUIRED"):
        second.switch("1111aaa")


def test_a_removed_release_is_simply_not_selectable(tmp_path: Path) -> None:
    """Pruned releases must vanish from selection, not resolve to a missing tree.

    In this store the manifest lives INSIDE the release directory, so removing the release
    removes its manifest too and `list_releases` stops offering it. That is why `switch`
    cannot be tricked into swapping `current` to a release that is not installed.
    """
    import shutil

    first, store, _ = _service(tmp_path, head="1111aaa")
    first.upgrade(tmp_path, activate=True)
    second, _, _ = _service(tmp_path, head="2222bbb", store=store)
    second.upgrade(tmp_path, activate=True)

    shutil.rmtree(store.release_path("1111aaa"))

    assert store.read_manifest("1111aaa") is None
    with pytest.raises(ConfigError, match="RELEASE_NOT_FOUND"):
        second.switch("1111aaa")
    # `current` must be untouched by a refused switch.
    assert store.current_sha() == "2222bbb"


# ------- unterminalized activations: forward path and honest unwind (#304)


def test_a_succeeded_activation_can_be_terminalized_without_rolling_back(
    tmp_path: Path,
) -> None:
    """The exact state #304 was reported from: `current` switched, runtime converged on
    the target, no terminal receipt. Rolling back would demote a healthy runtime, so
    reconciliation is the forward path -- and it must produce a real ACTIVATED receipt."""
    first, store, _ = _service(tmp_path, head="1111aaa")
    first.upgrade(tmp_path, activate=True)
    second, _, _ = _service(tmp_path, head="2222bbb", store=store)
    second.upgrade(tmp_path, activate=True)
    # Simulate the crash: the activation swapped `current` and the runtime adopted it,
    # but the process died before writing its receipt.
    store.begin_activation(receipt_id="act-20260725-900", from_sha="1111aaa", to_sha="2222bbb")
    store.record_activation_stage("symlink_switched")

    result = second.reconcile()

    assert result.status == "reconciled"
    assert result.active_sha == "2222bbb"
    assert result.converged is True
    # The receipt keeps the id the journal published, so the operator finds what they were told.
    assert result.activation_receipt == "act-20260725-900"
    receipt = store.read_receipt("act-20260725-900")
    assert receipt is not None
    assert receipt.outcome.value == "activated"
    assert receipt.stage.value == "health_verified"
    assert receipt.from_sha == "1111aaa"
    # The journal is terminal, so the runtime is unchanged and activations are unblocked.
    assert store.read_in_flight_activation() is None
    assert store.current_sha() == "2222bbb"


def test_reconciliation_unblocks_the_next_activation(tmp_path: Path) -> None:
    """The guard must not deadlock: a provably converged journal is terminalized in place
    rather than refusing a re-run while the only alternative demotes a healthy runtime."""
    first, store, _ = _service(tmp_path, head="1111aaa")
    first.upgrade(tmp_path, activate=True)
    store.begin_activation(receipt_id="act-20260725-901", from_sha=None, to_sha="1111aaa")
    store.record_activation_stage("symlink_switched")

    second, _, _ = _service(tmp_path, head="2222bbb", store=store)
    result = second.upgrade(tmp_path, activate=True)

    assert result.status == "activated"
    assert store.current_sha() == "2222bbb"
    # The stale journal was closed with its own receipt, not silently discarded.
    reconciled = store.read_receipt("act-20260725-901")
    assert reconciled is not None
    assert reconciled.outcome.value == "activated"
    assert reconciled.to_sha == "1111aaa"


def test_reconcile_refuses_a_target_the_runtime_is_not_serving(tmp_path: Path) -> None:
    first, store, _ = _service(tmp_path, head="1111aaa")
    first.upgrade(tmp_path, activate=True)
    second, _, _ = _service(tmp_path, head="2222bbb", store=store)
    second.upgrade(tmp_path, activate=True)
    # A journal whose target is NOT what `current` points at cannot be terminalized.
    store.begin_activation(receipt_id="act-20260725-902", from_sha="2222bbb", to_sha="1111aaa")

    with pytest.raises(ConfigError, match="ACTIVATION_NOT_RECONCILABLE"):
        second.reconcile()
    assert store.read_in_flight_activation() is not None
    assert store.current_sha() == "2222bbb"


def test_reconcile_refuses_when_the_runtime_is_unhealthy(tmp_path: Path) -> None:
    """Convergence is observed AND health-verified; a sick runtime is not an activation."""
    first, store, _ = _service(tmp_path, head="1111aaa")
    first.upgrade(tmp_path, activate=True)
    store.begin_activation(receipt_id="act-20260725-903", from_sha=None, to_sha="1111aaa")

    unhealthy = UpgradeService(
        store=store,
        inspector=_Inspector(WorktreeState(head_sha="1111aaa", clean=True)),
        builder=_Builder(),
        installer=_Installer(),
        smoke=_Smoke(),
        restarter=_Restarter(),
        observer=_Observer(store),
        clock=_Clock(),
        health_probe=_HealthProbe([HealthSample(healthy=False, detail="tunnel down")]),
        converge_attempts=1,
        converge_interval_seconds=0,
    )
    with pytest.raises(ConfigError, match="ACTIVATION_NOT_RECONCILABLE"):
        unhealthy.reconcile()
    assert store.read_in_flight_activation() is not None


def test_reconcile_is_a_no_op_when_nothing_is_in_flight(tmp_path: Path) -> None:
    service, _, restarter = _service(tmp_path, head="1111aaa")
    service.upgrade(tmp_path, activate=True)
    restarts_before = restarter.calls

    result = service.reconcile()

    assert result.status == "nothing_to_reconcile"
    assert result.active_sha == "1111aaa"
    assert restarter.calls == restarts_before


def test_reconcile_repair_rollback_recovers_a_fail_closed_target(tmp_path: Path) -> None:
    """#367 repair: a release that failed closed with a typed non-retryable failure is
    terminalized as a rollback when the previous release is still usable."""
    first, store, _ = _service(tmp_path, head="1111aaa")
    first.upgrade(tmp_path, activate=True)
    second, _, _ = _service(tmp_path, head="2222bbb", store=store)
    second.upgrade(tmp_path, activate=True)
    # The failed activation swapped `current` to the broken release, the runtime went
    # fail-closed with a typed contract identity error, and no receipt was written.
    store.begin_activation(receipt_id="act-20260725-910", from_sha="1111aaa", to_sha="2222bbb")
    store.record_activation_stage("symlink_switched")

    failing = UpgradeService(
        store=store,
        inspector=_Inspector(WorktreeState(head_sha="2222bbb", clean=True)),
        builder=_Builder(),
        installer=_Installer(),
        smoke=_Smoke(),
        restarter=_Restarter(),
        observer=_PhaseTrackingObserver(
            store, failed_sha="2222bbb", error_code="CONTRACT_ARTIFACT_MISMATCH"
        ),
        clock=_Clock(),
        converge_attempts=2,
        converge_interval_seconds=0,
    )

    result = failing.reconcile(repair="rollback")

    assert result.status == "rolled_back"
    assert store.current_sha() == "1111aaa"
    assert result.active_sha == "1111aaa"
    assert result.activation_receipt is not None
    receipt = store.read_receipt(result.activation_receipt)
    assert receipt is not None and receipt.outcome.value == "rolled_back"
    assert store.read_in_flight_activation() is None


def test_reconcile_repair_refuses_without_a_typed_non_retryable_failure(
    tmp_path: Path,
) -> None:
    """Repair is a mutation; it is only offered when the target provably failed closed."""
    first, store, _ = _service(tmp_path, head="1111aaa")
    first.upgrade(tmp_path, activate=True)
    second, _, _ = _service(tmp_path, head="2222bbb", store=store)
    second.upgrade(tmp_path, activate=True)
    store.begin_activation(receipt_id="act-20260725-911", from_sha="1111aaa", to_sha="2222bbb")
    store.record_activation_stage("symlink_switched")

    degraded = UpgradeService(
        store=store,
        inspector=_Inspector(WorktreeState(head_sha="2222bbb", clean=True)),
        builder=_Builder(),
        installer=_Installer(),
        smoke=_Smoke(),
        restarter=_Restarter(),
        observer=_Observer(store, pinned="9999fff", phase="degraded", last_error_code=None),
        clock=_Clock(),
        converge_attempts=2,
        converge_interval_seconds=0,
    )
    with pytest.raises(ConfigError, match="ACTIVATION_NOT_REPAIRABLE"):
        degraded.reconcile(repair="rollback")
    assert store.current_sha() == "2222bbb"
    assert store.read_in_flight_activation() is not None


def test_reconcile_repair_refuses_when_the_previous_release_is_unusable(
    tmp_path: Path,
) -> None:
    first, store, _ = _service(tmp_path, head="1111aaa")
    first.upgrade(tmp_path, activate=True)
    second, _, _ = _service(tmp_path, head="2222bbb", store=store)
    second.upgrade(tmp_path, activate=True)
    store.begin_activation(receipt_id="act-20260725-912", from_sha="1111aaa", to_sha="2222bbb")
    store.record_activation_stage("symlink_switched")
    (store.release_path("1111aaa") / ".manifest.json").unlink()

    failing = UpgradeService(
        store=store,
        inspector=_Inspector(WorktreeState(head_sha="2222bbb", clean=True)),
        builder=_Builder(),
        installer=_Installer(),
        smoke=_Smoke(),
        restarter=_Restarter(),
        observer=_Observer(
            store,
            phase="fail_closed",
            last_error_code="CONTRACT_ARTIFACT_MISMATCH",
            fail_closed_since="2026-07-25T10:06:00+00:00",
        ),
        clock=_Clock(),
        converge_attempts=2,
        converge_interval_seconds=0,
    )
    with pytest.raises(ConfigError, match="ACTIVATION_NOT_REPAIRABLE"):
        failing.reconcile(repair="rollback")
    assert store.current_sha() == "2222bbb"
    assert store.read_in_flight_activation() is not None


def test_reconcile_without_repair_still_refuses_a_fail_closed_target(tmp_path: Path) -> None:
    """Repair is opt-in: plain reconcile keeps its forward-only contract."""
    first, store, _ = _service(tmp_path, head="1111aaa")
    first.upgrade(tmp_path, activate=True)
    second, _, _ = _service(tmp_path, head="2222bbb", store=store)
    second.upgrade(tmp_path, activate=True)
    store.begin_activation(receipt_id="act-20260725-913", from_sha="1111aaa", to_sha="2222bbb")
    store.record_activation_stage("symlink_switched")

    failing = UpgradeService(
        store=store,
        inspector=_Inspector(WorktreeState(head_sha="2222bbb", clean=True)),
        builder=_Builder(),
        installer=_Installer(),
        smoke=_Smoke(),
        restarter=_Restarter(),
        observer=_Observer(
            store, phase="fail_closed", last_error_code="CONTRACT_ARTIFACT_MISMATCH"
        ),
        clock=_Clock(),
        converge_attempts=2,
        converge_interval_seconds=0,
    )
    with pytest.raises(ConfigError, match="ACTIVATION_NOT_RECONCILABLE"):
        failing.reconcile()
    assert store.read_in_flight_activation() is not None


def test_a_failed_activation_whose_rollback_also_fails_says_so(tmp_path: Path) -> None:
    """#304 defect 2: an unwind that did not converge must never be reported as a
    rollback, and the operator must be told what state the runtime is actually in."""
    first, store, _ = _service(tmp_path, head="1111aaa")
    first.upgrade(tmp_path, activate=True)

    class _NeverServing:
        """A runtime that publishes no release identity at all: nothing can converge."""

        def observe(self) -> ObservedRuntime:
            return ObservedRuntime(running_release_sha=None, phase="failed", pid=None)

    stuck = UpgradeService(
        store=store,
        inspector=_Inspector(WorktreeState(head_sha="2222bbb", clean=True)),
        builder=_Builder(),
        installer=_Installer(),
        smoke=_Smoke(),
        restarter=_Restarter(ok=False),
        observer=_NeverServing(),
        clock=_Clock(),
        converge_attempts=1,
        converge_interval_seconds=0,
    )
    with pytest.raises(ConfigError, match="ACTIVATION_FAILED_ROLLBACK_FAILED") as raised:
        stuck.upgrade(tmp_path, activate=True)

    message = str(raised.value)
    assert "did not converge either" in message
    # Both dispositions are durable, and neither claims success.
    outcomes = sorted(r.outcome.value for r in store.list_receipts())
    assert "failed" in outcomes
    assert "rollback_failed" in outcomes
    assert store.read_in_flight_activation() is None


def test_a_rollback_target_that_cannot_be_verified_is_reported_not_swallowed(
    tmp_path: Path,
) -> None:
    """The unwind raised before touching anything (a pruned target). The activation must
    still end in a terminal receipt plus an actionable error -- never a claimed rollback."""
    import shutil

    first, store, _ = _service(tmp_path, head="1111aaa")
    first.upgrade(tmp_path, activate=True)
    second, _, _ = _service(tmp_path, head="2222bbb", store=store, restarter=_Restarter(ok=False))
    # Destroy the only rollback target after it became `previous`.
    shutil.rmtree(store.release_path("1111aaa"))

    with pytest.raises(ConfigError, match="ACTIVATION_FAILED_ROLLBACK_FAILED") as raised:
        second.upgrade(tmp_path, activate=True)

    message = str(raised.value)
    assert "ROLLBACK_TARGET_UNUSABLE" in message
    assert "rf version status" in message
    assert [r.outcome.value for r in store.list_receipts()].count("failed") == 1
    assert store.read_in_flight_activation() is None


def test_reconcile_repair_refuses_a_transient_failure_code(tmp_path: Path) -> None:
    """Repair is only authorized for deterministic contract failures (#420)."""
    first, store, _ = _service(tmp_path, head="1111aaa")
    first.upgrade(tmp_path, activate=True)
    second, _, _ = _service(tmp_path, head="2222bbb", store=store)
    second.upgrade(tmp_path, activate=True)
    store.begin_activation(receipt_id="act-20260725-914", from_sha="1111aaa", to_sha="2222bbb")
    store.record_activation_stage("symlink_switched")

    failing = UpgradeService(
        store=store,
        inspector=_Inspector(WorktreeState(head_sha="2222bbb", clean=True)),
        builder=_Builder(),
        installer=_Installer(),
        smoke=_Smoke(),
        restarter=_Restarter(),
        observer=_Observer(
            store,
            phase="fail_closed",
            last_error_code="TUNNEL_DOCTOR_FAILED",
            fail_closed_since="2026-07-25T10:06:00+00:00",
        ),
        clock=_Clock(),
        converge_attempts=2,
        converge_interval_seconds=0,
    )
    with pytest.raises(ConfigError, match="ACTIVATION_NOT_REPAIRABLE"):
        failing.reconcile(repair="rollback")
    assert store.read_in_flight_activation() is not None


def test_reconcile_repair_requires_the_runtime_to_be_the_journal_target(
    tmp_path: Path,
) -> None:
    """The observed runtime must be the release the journal names, not just `current`."""
    first, store, _ = _service(tmp_path, head="1111aaa")
    first.upgrade(tmp_path, activate=True)
    second, _, _ = _service(tmp_path, head="2222bbb", store=store)
    second.upgrade(tmp_path, activate=True)
    store.begin_activation(receipt_id="act-20260725-915", from_sha="1111aaa", to_sha="2222bbb")
    store.record_activation_stage("symlink_switched")

    failing = UpgradeService(
        store=store,
        inspector=_Inspector(WorktreeState(head_sha="2222bbb", clean=True)),
        builder=_Builder(),
        installer=_Installer(),
        smoke=_Smoke(),
        restarter=_Restarter(),
        observer=_Observer(
            store,
            pinned="9999fff",
            phase="fail_closed",
            last_error_code="CONTRACT_ARTIFACT_MISMATCH",
            fail_closed_since="2026-07-25T10:06:00+00:00",
        ),
        clock=_Clock(),
        converge_attempts=2,
        converge_interval_seconds=0,
    )
    with pytest.raises(ConfigError, match="ACTIVATION_NOT_REPAIRABLE"):
        failing.reconcile(repair="rollback")
    assert store.read_in_flight_activation() is not None


def test_reconcile_repair_requires_fail_closed_since(tmp_path: Path) -> None:
    """A fail_closed phase without a durable fail-closed marker is not repairable."""
    first, store, _ = _service(tmp_path, head="1111aaa")
    first.upgrade(tmp_path, activate=True)
    second, _, _ = _service(tmp_path, head="2222bbb", store=store)
    second.upgrade(tmp_path, activate=True)
    store.begin_activation(receipt_id="act-20260725-916", from_sha="1111aaa", to_sha="2222bbb")
    store.record_activation_stage("symlink_switched")

    failing = UpgradeService(
        store=store,
        inspector=_Inspector(WorktreeState(head_sha="2222bbb", clean=True)),
        builder=_Builder(),
        installer=_Installer(),
        smoke=_Smoke(),
        restarter=_Restarter(),
        observer=_Observer(
            store,
            phase="fail_closed",
            last_error_code="CONTRACT_ARTIFACT_MISMATCH",
            fail_closed_since=None,
        ),
        clock=_Clock(),
        converge_attempts=2,
        converge_interval_seconds=0,
    )
    with pytest.raises(ConfigError, match="ACTIVATION_NOT_REPAIRABLE"):
        failing.reconcile(repair="rollback")
    assert store.read_in_flight_activation() is not None


def test_rollback_re_smokes_the_target_before_swapping(tmp_path: Path) -> None:
    """A rollback whose target fails contract re-verification must not swap (#420)."""
    first, store, _ = _service(tmp_path, head="1111aaa")
    first.upgrade(tmp_path, activate=True)
    second, _, _ = _service(tmp_path, head="2222bbb", store=store)
    second.upgrade(tmp_path, activate=True)

    broken = UpgradeService(
        store=store,
        inspector=_Inspector(WorktreeState(head_sha="2222bbb", clean=True)),
        builder=_Builder(),
        installer=_Installer(),
        smoke=_Smoke(mismatched_fields=("input_contract_digest",)),
        restarter=_Restarter(),
        observer=_Observer(store),
        clock=_Clock(),
        converge_attempts=2,
        converge_interval_seconds=0,
    )
    with pytest.raises(ConfigError, match="RELEASE_CONTRACT_IDENTITY_MISMATCH"):
        broken.rollback()
    # `current` must be untouched: the target was not proven safe to serve.
    assert store.current_sha() == "2222bbb"
    assert store.read_in_flight_activation() is None


def test_rollback_force_unverified_is_an_explicit_escape_hatch(tmp_path: Path) -> None:
    """--force-unverified skips the pre-swap re-smoke, never the health verification."""
    first, store, _ = _service(tmp_path, head="1111aaa")
    first.upgrade(tmp_path, activate=True)
    second, _, _ = _service(tmp_path, head="2222bbb", store=store)
    second.upgrade(tmp_path, activate=True)

    broken = UpgradeService(
        store=store,
        inspector=_Inspector(WorktreeState(head_sha="2222bbb", clean=True)),
        builder=_Builder(),
        installer=_Installer(),
        smoke=_Smoke(mismatched_fields=("input_contract_digest",)),
        restarter=_Restarter(),
        observer=_Observer(store),
        clock=_Clock(),
        converge_attempts=2,
        converge_interval_seconds=0,
    )
    result = broken.rollback(force_unverified=True)

    assert result.status == "rolled_back"
    assert store.current_sha() == "1111aaa"
    # The receipt still requires observed convergence -- force-unverified only skips
    # the pre-swap probe, never the health-verified truthfulness of the receipt.
    receipt = store.read_receipt(result.activation_receipt)
    assert receipt is not None and receipt.converged is True
