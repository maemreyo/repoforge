from __future__ import annotations

from pathlib import Path

from repoforge.adapters.activation.release_store import RuntimeReleaseStore
from repoforge.application.activation.version_status import (
    RuntimeIdentityInputs,
    build_version_list,
    build_version_status,
)
from repoforge.domain.activation import ReleaseManifest

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


def test_status_reports_the_active_commit_and_manifest(tmp_path: Path) -> None:
    store = RuntimeReleaseStore(tmp_path)
    _install(store, "aaa1111", surface=_SURFACE, built_at="2026-07-25T09:00:00+00:00")
    store.swap_current("aaa1111")

    status = build_version_status(
        store,
        RuntimeIdentityInputs(launcher_version="2.2.0", running_tool_surface_hash=_SURFACE),
    )

    assert status["active_commit"] == "aaa1111"
    assert status["tool_surface_hash"] == _SURFACE
    assert status["client_rediscovery_required"] is False


def test_status_flags_rediscovery_when_installed_surface_differs_from_running(
    tmp_path: Path,
) -> None:
    store = RuntimeReleaseStore(tmp_path)
    _install(store, "aaa1111", surface=_SURFACE_NEW, built_at="2026-07-25T09:00:00+00:00")
    store.swap_current("aaa1111")

    status = build_version_status(
        store,
        RuntimeIdentityInputs(running_tool_surface_hash=_SURFACE),
    )

    assert status["client_rediscovery_required"] is True
    assert "rediscover" in str(status["safe_next_action"]).lower()


def test_status_without_any_release_points_at_upgrade(tmp_path: Path) -> None:
    store = RuntimeReleaseStore(tmp_path)
    status = build_version_status(store, RuntimeIdentityInputs())
    assert status["active_commit"] is None
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
    # Newest first.
    assert releases[0]["commit_sha"] == "bbb2222"
    assert releases[0]["current"] is True
    assert releases[1]["previous"] is True
