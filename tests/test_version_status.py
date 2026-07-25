from __future__ import annotations

from pathlib import Path

from repoforge.adapters.activation.release_store import RuntimeReleaseStore
from repoforge.application.activation.version_status import (
    RuntimeIdentityInputs,
    build_version_list,
    build_version_status,
)
from repoforge.domain.activation import (
    ActivationOutcome,
    ActivationReceipt,
    ActivationStage,
    ReleaseManifest,
)
from repoforge.ports.activation import ObservedRuntime

_FINGERPRINT = "a" * 64
_SURFACE = "b" * 64
_SURFACE_NEW = "c" * 64


def _install(store: RuntimeReleaseStore, commit: str, *, surface: str, built_at: str) -> None:
    store.release_path(commit).mkdir(parents=True, exist_ok=True)
    store.write_manifest(
        ReleaseManifest(
            commit_sha=commit,
            package_version="2.2.0",
            build_fingerprint=_FINGERPRINT,
            tool_surface_hash=surface,
            source_worktree="/src",
            built_at=built_at,
        )
    )


def _observed(sha: str | None, *, phase: str = "healthy") -> ObservedRuntime:
    return ObservedRuntime(running_release_sha=sha, phase=phase, pid=42, executable="/x/python")


def _receipt(store: RuntimeReleaseStore, *, to_sha: str, rediscovery: bool) -> None:
    store.write_receipt(
        ActivationReceipt(
            receipt_id=store.allocate_receipt_id(date_stamp="20260725"),
            from_sha=None,
            to_sha=to_sha,
            to_fingerprint=_FINGERPRINT,
            tool_surface_hash=_SURFACE,
            rediscovery_required=rediscovery,
            outcome=ActivationOutcome.ACTIVATED,
            activated_at="2026-07-25T09:00:00+00:00",
            stage=ActivationStage.HEALTH_VERIFIED,
            observed_sha=to_sha,
            converged=True,
        )
    )


def test_status_reports_the_active_commit_when_desired_and_observed_agree(
    tmp_path: Path,
) -> None:
    store = RuntimeReleaseStore(tmp_path)
    _install(store, "aaa1111", surface=_SURFACE, built_at="2026-07-25T09:00:00+00:00")
    store.swap_current("aaa1111")

    status = build_version_status(
        store,
        RuntimeIdentityInputs(launcher_version="2.2.0"),
        _observed("aaa1111"),
    )

    assert status["desired_commit"] == "aaa1111"
    assert status["observed_commit"] == "aaa1111"
    assert status["active_commit"] == "aaa1111"
    assert status["activation_converged"] is True
    assert status["safe_next_action"] == "Runtime identity is current."


def test_status_fails_closed_when_the_runtime_serves_a_different_release(
    tmp_path: Path,
) -> None:
    """The core acceptance: never report the symlink's wish as the running commit."""
    store = RuntimeReleaseStore(tmp_path)
    _install(store, "aaa0111", surface=_SURFACE, built_at="2026-07-25T08:00:00+00:00")
    _install(store, "bbb0222", surface=_SURFACE_NEW, built_at="2026-07-25T09:00:00+00:00")
    store.swap_current("aaa0111")
    store.swap_current("bbb0222")

    # `current` is the candidate, but the live process is still the old release.
    status = build_version_status(store, RuntimeIdentityInputs(), _observed("aaa0111"))

    assert status["desired_commit"] == "bbb0222"
    assert status["observed_commit"] == "aaa0111"
    # Must NOT claim the candidate is active.
    assert status["active_commit"] is None
    assert status["activation_converged"] is False
    assert "ACTIVATION NOT CONVERGED" in str(status["safe_next_action"])
    # Surfaces differ between desired and observed -> rediscovery needed.
    assert status["client_rediscovery_required"] is True


def test_status_reports_no_running_runtime_distinctly(tmp_path: Path) -> None:
    store = RuntimeReleaseStore(tmp_path)
    _install(store, "aaa1111", surface=_SURFACE, built_at="2026-07-25T09:00:00+00:00")
    store.swap_current("aaa1111")

    status = build_version_status(store, RuntimeIdentityInputs(), _observed(None, phase="stopped"))

    assert status["activation_converged"] is False
    assert status["active_commit"] is None
    assert "no runtime is running" in str(status["safe_next_action"])


def test_rediscovery_comes_from_the_activation_receipt_not_a_live_hash_compare(
    tmp_path: Path,
) -> None:
    """Once the new runtime is up the hashes match again; the flag must still hold."""
    store = RuntimeReleaseStore(tmp_path)
    _install(store, "aaa1111", surface=_SURFACE, built_at="2026-07-25T09:00:00+00:00")
    store.swap_current("aaa1111")
    _receipt(store, to_sha="aaa1111", rediscovery=True)

    status = build_version_status(
        store,
        RuntimeIdentityInputs(running_tool_surface_hash=_SURFACE),
        _observed("aaa1111"),
    )

    assert status["activation_converged"] is True
    assert status["client_rediscovery_required"] is True
    assert "rediscover" in str(status["safe_next_action"]).lower()


def test_status_without_any_release_points_at_upgrade(tmp_path: Path) -> None:
    store = RuntimeReleaseStore(tmp_path)
    status = build_version_status(store, RuntimeIdentityInputs(), _observed(None, phase="stopped"))
    assert status["desired_commit"] is None
    assert "rf upgrade" in str(status["safe_next_action"])


def test_list_marks_current_and_previous(tmp_path: Path) -> None:
    store = RuntimeReleaseStore(tmp_path)
    _install(store, "aaa1111", surface=_SURFACE, built_at="2026-07-25T09:00:00+00:00")
    _install(store, "bbb2222", surface=_SURFACE, built_at="2026-07-25T10:00:00+00:00")
    store.swap_current("aaa1111")
    store.swap_current("bbb2222")

    listing = build_version_list(store)

    assert listing["current"] == "bbb2222"
    assert listing["previous"] == "aaa1111"
    releases = listing["releases"]
    assert isinstance(releases, list)
    assert releases[0]["commit_sha"] == "bbb2222"
    assert releases[0]["current"] is True
    assert releases[1]["previous"] is True


def test_a_stopped_runtime_with_a_release_executable_is_not_converged(
    tmp_path: Path,
) -> None:
    """Round-2 finding 6: a STOPPED record must never read as an active release."""
    store = RuntimeReleaseStore(tmp_path)
    _install(store, "aaa1111", surface=_SURFACE, built_at="2026-07-25T09:00:00+00:00")
    store.swap_current("aaa1111")

    # A production STOPPED record still carries the release executable, but has no pid.
    stopped = ObservedRuntime(
        running_release_sha=None,
        phase="stopped",
        pid=None,
        executable=str(store.release_path("aaa1111") / "venv" / "bin" / "python"),
    )
    status = build_version_status(store, RuntimeIdentityInputs(), stopped)

    assert status["activation_converged"] is False
    assert status["active_commit"] is None
    assert "no runtime is running" in str(status["safe_next_action"])


def test_status_reports_degraded_history_when_the_newest_receipt_is_corrupt(
    tmp_path: Path,
) -> None:
    """Round-3 finding 4: status must not silently fall back to an older receipt."""
    import json

    store = RuntimeReleaseStore(tmp_path)
    _install(store, "aaa1111", surface=_SURFACE, built_at="2026-07-25T09:00:00+00:00")
    store.swap_current("aaa1111")
    _receipt(store, to_sha="aaa1111", rediscovery=False)
    receipts = store.root / "runtime" / "activation-receipts"
    (receipts / "act-20260725-009.json").write_text("{corrupt", encoding="utf-8")
    assert json  # keep the import meaningful for readers

    status = build_version_status(store, RuntimeIdentityInputs(), _observed("aaa1111"))

    assert status["receipt_history_degraded"] is True
    assert status["unreadable_receipts"] == ["act-20260725-009"]
    # No lie about which activation was last.
    assert status["activation_receipt"] is None
    assert "unreadable" in str(status["safe_next_action"])
