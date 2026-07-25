from __future__ import annotations

import os
from pathlib import Path

import pytest

from repoforge.adapters.activation.release_store import RuntimeReleaseStore
from repoforge.domain.activation import (
    ActivationOutcome,
    ActivationReceipt,
    ActivationStage,
    ReleaseManifest,
)
from repoforge.domain.errors import ConfigError

_SHA_A = "a" * 64
_SHA_B = "b" * 64


def _install(
    store: RuntimeReleaseStore, commit: str, *, built_at: str, surface: str = _SHA_B
) -> None:
    """Materialize a fake release directory and its manifest."""
    (store.release_path(commit) / "venv" / "bin").mkdir(parents=True, exist_ok=True)
    store.write_manifest(
        ReleaseManifest(
            commit_sha=commit,
            package_version="2.2.0",
            build_fingerprint=_SHA_A,
            tool_surface_hash=surface,
            source_worktree="/src",
            built_at=built_at,
        )
    )


def test_swap_current_demotes_the_old_release_to_previous(tmp_path: Path) -> None:
    store = RuntimeReleaseStore(tmp_path)
    _install(store, "aaa1111", built_at="2026-07-25T09:00:00+00:00")
    _install(store, "bbb2222", built_at="2026-07-25T10:00:00+00:00")

    assert store.swap_current("aaa1111") is None
    assert store.current_sha() == "aaa1111"

    previous = store.swap_current("bbb2222")
    assert previous == "aaa1111"
    assert store.current_sha() == "bbb2222"
    assert store.previous_sha() == "aaa1111"


def test_current_is_a_real_symlink_pointing_at_the_release(tmp_path: Path) -> None:
    store = RuntimeReleaseStore(tmp_path)
    _install(store, "aaa1111", built_at="2026-07-25T09:00:00+00:00")
    store.swap_current("aaa1111")
    link = tmp_path / "current"
    assert link.is_symlink()
    assert os.readlink(link) == str(Path("releases") / "aaa1111")


def test_rollback_swaps_current_and_previous(tmp_path: Path) -> None:
    store = RuntimeReleaseStore(tmp_path)
    _install(store, "aaa1111", built_at="2026-07-25T09:00:00+00:00")
    _install(store, "bbb2222", built_at="2026-07-25T10:00:00+00:00")
    store.swap_current("aaa1111")
    store.swap_current("bbb2222")

    now_active = store.rollback()
    assert now_active == "aaa1111"
    assert store.current_sha() == "aaa1111"
    assert store.previous_sha() == "bbb2222"


def test_rollback_without_a_previous_release_is_refused(tmp_path: Path) -> None:
    store = RuntimeReleaseStore(tmp_path)
    _install(store, "aaa1111", built_at="2026-07-25T09:00:00+00:00")
    store.swap_current("aaa1111")
    with pytest.raises(ConfigError, match="NO_PREVIOUS_RELEASE"):
        store.rollback()


def test_swap_to_a_missing_release_is_refused(tmp_path: Path) -> None:
    store = RuntimeReleaseStore(tmp_path)
    with pytest.raises(ConfigError, match="RELEASE_NOT_INSTALLED"):
        store.swap_current("deadbee")


def test_prune_keeps_current_previous_and_newest(tmp_path: Path) -> None:
    store = RuntimeReleaseStore(tmp_path)
    for index, commit in enumerate(("aaa1111", "bbb2222", "ccc3333", "ddd4444")):
        _install(store, commit, built_at=f"2026-07-25T0{index}:00:00+00:00")
    # oldest -> current, second-oldest -> previous
    store.swap_current("aaa1111")
    store.swap_current("bbb2222")

    removed = store.prune(keep=1)

    # keep=1 retains the single newest (ddd4444); current (bbb2222) and previous
    # (aaa1111) are always protected; only ccc3333 is eligible for removal.
    assert removed == ["ccc3333"]
    assert not store.release_path("ccc3333").exists()
    assert store.release_path("aaa1111").exists()
    assert store.release_path("bbb2222").exists()
    assert store.release_path("ddd4444").exists()


def test_receipts_are_written_read_and_allocated_monotonically(tmp_path: Path) -> None:
    store = RuntimeReleaseStore(tmp_path)
    first = store.allocate_receipt_id(date_stamp="20260725")
    assert first == "act-20260725-001"
    receipt = ActivationReceipt(
        receipt_id=first,
        from_sha=None,
        to_sha="aaa1111",
        to_fingerprint=_SHA_A,
        tool_surface_hash=_SHA_B,
        rediscovery_required=False,
        outcome=ActivationOutcome.ACTIVATED,
        activated_at="2026-07-25T10:00:00+00:00",
        stage=ActivationStage.HEALTH_VERIFIED,
        observed_sha="aaa1111",
        converged=True,
    )
    store.write_receipt(receipt)
    assert store.read_receipt(first) == receipt
    assert store.allocate_receipt_id(date_stamp="20260725") == "act-20260725-002"


def test_internal_shim_is_executable_and_resolves_through_current(tmp_path: Path) -> None:
    store = RuntimeReleaseStore(tmp_path)
    shim = store.write_internal_launcher_shim()
    assert shim == store.bin_launcher()
    assert os.access(shim, os.X_OK)
    script = shim.read_text(encoding="utf-8")
    # Resolves `current` once, then execs the CONCRETE release while publishing the
    # captured sha, so the runtime identity cannot drift with a later swap.
    assert 'readlink "$root/current"' in script
    assert "REPOFORGE_RUNNING_RELEASE_SHA" in script
    assert "releases/$sha/venv/bin/rf" in script


def test_a_store_without_a_path_launcher_never_provisions_one(tmp_path: Path) -> None:
    """A temporary release root must not be able to touch ~/.local/bin/rf."""
    store = RuntimeReleaseStore(tmp_path)
    assert store.path_launcher() is None
    assert store.install_path_launcher() is None


def test_path_launcher_is_provisioned_only_when_granted(tmp_path: Path) -> None:
    target = tmp_path / "home-bin" / "rf"
    store = RuntimeReleaseStore(tmp_path, path_launcher=target)
    assert store.install_path_launcher() == target
    assert "repoforge-launcher:v1" in target.read_text(encoding="utf-8")


def test_reserve_release_refuses_to_overwrite_different_bits(tmp_path: Path) -> None:
    store = RuntimeReleaseStore(tmp_path)
    _install(store, "aaa1111", built_at="2026-07-25T09:00:00+00:00")
    # Same commit, identical fingerprint -> already installed, no reinstall needed.
    assert store.reserve_release("aaa1111", build_fingerprint=_SHA_A) is False
    # Same commit, different fingerprint -> refuse rather than write over it.
    with pytest.raises(ConfigError, match="RELEASE_FINGERPRINT_CONFLICT"):
        store.reserve_release("aaa1111", build_fingerprint="d" * 64)
    # A fresh commit is claimable.
    assert store.reserve_release("bbb2222", build_fingerprint=_SHA_A) is True


# ------------------------------------------- legacy receipts (round-2 finding 5)

_LEGACY_ACTIVATED = {
    # Exactly the shape written before the convergence fields existed.
    "receipt_id": "act-20260724-001",
    "from_sha": None,
    "to_sha": "aaa1111",
    "from_fingerprint": None,
    "to_fingerprint": _SHA_A,
    "tool_surface_hash": _SHA_B,
    "rediscovery_required": False,
    "outcome": "activated",
    "activated_at": "2026-07-24T10:00:00+00:00",
    "detail": "Activated; supervisor reloaded.",
}


def _write_raw_receipt(store: RuntimeReleaseStore, payload: dict[str, object]) -> None:
    import json

    path = store.root / "runtime" / "activation-receipts" / f"{payload['receipt_id']}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_a_pre_convergence_receipt_is_still_readable(tmp_path: Path) -> None:
    """A stored receipt must never become unreadable after an upgrade."""
    store = RuntimeReleaseStore(tmp_path)
    _write_raw_receipt(store, dict(_LEGACY_ACTIVATED))

    receipt = store.read_receipt("act-20260724-001")

    assert receipt is not None
    # Readable, but explicitly NOT counted as verified.
    assert receipt.stage is ActivationStage.LEGACY_UNKNOWN
    assert receipt.converged is False
    assert store.list_receipts()[0].receipt_id == "act-20260724-001"


def test_a_corrupt_receipt_does_not_break_the_history_or_allocation(tmp_path: Path) -> None:
    """One bad file must not take down version status or receipt allocation."""
    store = RuntimeReleaseStore(tmp_path)
    _write_raw_receipt(store, dict(_LEGACY_ACTIVATED))
    bad = store.root / "runtime" / "activation-receipts" / "act-20260724-002.json"
    bad.write_text("{not json", encoding="utf-8")

    # History still lists the readable one.
    assert [r.receipt_id for r in store.list_receipts()] == ["act-20260724-001"]
    # Allocation still avoids BOTH ids on disk, including the unparseable one.
    assert store.allocate_receipt_id(date_stamp="20260724") == "act-20260724-003"


def test_launcher_shim_migrates_a_legacy_uv_tool_entry_point(tmp_path: Path) -> None:
    """Round-2 finding 9.3: an existing uv-tool `rf` must be migrated, not skipped."""
    store = RuntimeReleaseStore(tmp_path)
    legacy = store.bin_launcher()
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text(
        "#!/usr/bin/env python\nfrom repoforge.interfaces.cli.main import main\n",
        encoding="utf-8",
    )

    written = store.write_internal_launcher_shim()

    assert written == legacy
    assert "repoforge-launcher:v1" in legacy.read_text(encoding="utf-8")


def test_launcher_shim_refuses_to_clobber_an_unrecognized_file(tmp_path: Path) -> None:
    store = RuntimeReleaseStore(tmp_path)
    occupied = store.bin_launcher()
    occupied.parent.mkdir(parents=True, exist_ok=True)
    occupied.write_text("some user script\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="LAUNCHER_PATH_OCCUPIED"):
        store.write_internal_launcher_shim()


# --------------------------------- degraded receipt history (round-3 finding 4)


def test_a_corrupt_newest_receipt_makes_history_degraded_with_no_latest(
    tmp_path: Path,
) -> None:
    """Never report an older receipt as "latest" when a newer one is unreadable."""
    store = RuntimeReleaseStore(tmp_path)
    _write_raw_receipt(store, dict(_LEGACY_ACTIVATED))  # act-20260724-001, readable
    newest = store.root / "runtime" / "activation-receipts" / "act-20260724-009.json"
    newest.write_text("{corrupt", encoding="utf-8")

    history = store.receipt_history()

    assert history.degraded is True
    assert history.unreadable == ("act-20260724-009",)
    # The readable one is still listed, but it is NOT the latest.
    assert [r.receipt_id for r in history.valid] == ["act-20260724-001"]
    assert history.latest is None


def test_an_older_corrupt_receipt_does_not_hide_a_readable_newest(tmp_path: Path) -> None:
    store = RuntimeReleaseStore(tmp_path)
    newer = dict(_LEGACY_ACTIVATED)
    newer["receipt_id"] = "act-20260724-009"
    _write_raw_receipt(store, newer)
    older = store.root / "runtime" / "activation-receipts" / "act-20260724-001.json"
    older.write_text("{corrupt", encoding="utf-8")

    history = store.receipt_history()

    assert history.degraded is True
    # The newest is readable, so reporting it as latest is truthful.
    assert history.latest is not None
    assert history.latest.receipt_id == "act-20260724-009"


def test_receipt_ordering_is_numeric_across_the_thousand_boundary(tmp_path: Path) -> None:
    """Round-4 finding 7: lexicographic order would rank 999 above 1000."""
    store = RuntimeReleaseStore(tmp_path)
    for sequence in ("998", "999", "1000", "1001"):
        payload = dict(_LEGACY_ACTIVATED)
        payload["receipt_id"] = f"act-20260725-{sequence}"
        _write_raw_receipt(store, payload)

    history = store.receipt_history()

    assert history.valid[0].receipt_id == "act-20260725-1001"
    assert history.latest is not None
    assert history.latest.receipt_id == "act-20260725-1001"


def test_a_corrupt_thousandth_receipt_is_recognised_as_the_newest(tmp_path: Path) -> None:
    store = RuntimeReleaseStore(tmp_path)
    payload = dict(_LEGACY_ACTIVATED)
    payload["receipt_id"] = "act-20260725-999"
    _write_raw_receipt(store, payload)
    corrupt = store.root / "runtime" / "activation-receipts" / "act-20260725-1000.json"
    corrupt.write_text("{corrupt", encoding="utf-8")

    history = store.receipt_history()

    # 1000 > 999 numerically, so the unreadable one IS the newest -> no truthful latest.
    assert history.degraded is True
    assert history.latest is None


def test_a_stale_shim_target_is_rewritten_even_though_the_marker_matches(
    tmp_path: Path,
) -> None:
    """Round-4 finding 8: the marker alone does not prove the shim targets this root."""
    store = RuntimeReleaseStore(tmp_path)
    shim = store.bin_launcher()
    shim.parent.mkdir(parents=True, exist_ok=True)
    # Our marker, but an absolute target from a DIFFERENT (moved) release root.
    shim.write_text(
        "#!/bin/sh\n# repoforge-launcher:v1 stale RepoForge stable launcher\n"
        'exec "/somewhere/else/current/venv/bin/rf" "$@"\n',
        encoding="utf-8",
    )

    store.write_internal_launcher_shim()

    script = shim.read_text(encoding="utf-8")
    assert "/somewhere/else/" not in script
    assert str(tmp_path) in script


# ------------------------- durable agent secret (round-5 finding 3)


def test_agent_env_is_owner_only_and_sourceable(tmp_path: Path) -> None:
    """launchd has no shell environment, so the secret must live in an owner-only file."""
    store = RuntimeReleaseStore(tmp_path)
    path = store.write_agent_env({"CONTROL_PLANE_API_KEY": "s3cret-with-'quote"})

    assert path == store.agent_env_path()
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    # Only names are exposed by the query API; the value is never returned.
    assert store.agent_env_keys() == {"CONTROL_PLANE_API_KEY"}
    # `sh -c '. file; echo $VAR'` must recover the exact value, quotes included.
    import subprocess

    out = subprocess.run(
        ["/bin/sh", "-c", f'. "{path}"; printf "%s" "$CONTROL_PLANE_API_KEY"'],
        capture_output=True,
        text=True,
        check=True,
    )
    assert out.stdout == "s3cret-with-'quote"


def test_agent_env_rejects_a_multiline_value(tmp_path: Path) -> None:
    store = RuntimeReleaseStore(tmp_path)
    with pytest.raises(ConfigError, match="AGENT_ENV_VALUE_INVALID"):
        store.write_agent_env({"CONTROL_PLANE_API_KEY": "line1\nline2"})


def test_the_supervisor_shim_sources_the_durable_secret(tmp_path: Path) -> None:
    store = RuntimeReleaseStore(tmp_path)
    script = store.write_supervisor_shim().read_text(encoding="utf-8")
    assert "runtime/agent.env" in script
    # Sourced BEFORE exec, so the worker inherits it.
    assert script.index("agent.env") < script.index("exec env")
