"""Naming releases the way a human remembers them, and the unified `rf runtime ls` view.

The operator's actual complaint was "I don't even remember which runtimes exist" -- with
every release identified by a 40-character sha and the state split across three commands,
a listing could not answer it. These are the regressions for the two halves of the fix:
memorable selection (branch / short sha) and one assembled view.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from repoforge.adapters.activation.release_store import RuntimeReleaseStore
from repoforge.application.activation.inventory import build_runtime_inventory
from repoforge.application.activation.selection import (
    release_choices,
    resolve_receipt_id,
    resolve_release,
)
from repoforge.domain.activation import (
    ActivationOutcome,
    ActivationReceipt,
    ActivationStage,
    ReleaseManifest,
)
from repoforge.domain.errors import ConfigError
from repoforge.ports.activation import ObservedRuntime
from repoforge.ports.process_supervisor import RegistrarStatus

_FINGERPRINT = "a" * 64
_SURFACE = "b" * 64


def _install(
    store: RuntimeReleaseStore,
    commit: str,
    *,
    branch: str = "",
    subject: str = "",
    built_at: str = "2026-07-25T10:00:00+00:00",
) -> ReleaseManifest:
    (store.release_path(commit) / "venv" / "bin").mkdir(parents=True, exist_ok=True)
    manifest = ReleaseManifest(
        commit_sha=commit,
        package_version="2.2.0",
        build_fingerprint=_FINGERPRINT,
        tool_surface_hash=_SURFACE,
        source_worktree="/src",
        built_at=built_at,
        branch=branch,
        subject=subject,
    )
    store.write_manifest(manifest)
    return manifest


# --------------------------------------------------------------- selection


def test_a_release_is_selectable_by_branch_name(tmp_path: Path) -> None:
    store = RuntimeReleaseStore(tmp_path)
    _install(store, "aaa1111", branch="main")
    _install(store, "bbb2222", branch="feat/activation", built_at="2026-07-25T11:00:00+00:00")

    assert resolve_release(store, "feat/activation").commit_sha == "bbb2222"
    assert resolve_release(store, "main").commit_sha == "aaa1111"


def test_a_release_is_selectable_by_short_sha_like_git(tmp_path: Path) -> None:
    store = RuntimeReleaseStore(tmp_path)
    _install(store, "aaa1111", branch="main")
    _install(store, "bbb2222", branch="dev")

    assert resolve_release(store, "bbb2").commit_sha == "bbb2222"
    assert resolve_release(store, "bbb2222").commit_sha == "bbb2222"


def test_a_prefix_shorter_than_git_would_accept_says_why(tmp_path: Path) -> None:
    """A 1-3 character "selection" would pick whatever sorts first, so it is refused.

    And it is refused with its OWN error: reporting "not found" for a selector that plainly
    prefix-matches would send the reader hunting for a release sitting in the listing.
    """
    store = RuntimeReleaseStore(tmp_path)
    _install(store, "aaa1111", branch="main")

    with pytest.raises(ConfigError, match="RELEASE_SELECTOR_TOO_SHORT") as short:
        resolve_release(store, "aa")
    assert "at least 4" in str(short.value)


def test_an_ambiguous_selector_is_refused_rather_than_guessed(tmp_path: Path) -> None:
    """Guessing here would switch the live runtime to a release nobody named."""
    store = RuntimeReleaseStore(tmp_path)
    _install(store, "aaaa111", branch="topic")
    _install(store, "aaaa222", branch="topic")

    with pytest.raises(ConfigError, match="RELEASE_SELECTOR_AMBIGUOUS") as ambiguous_branch:
        resolve_release(store, "topic")
    # The message must name the candidates, or the operator cannot proceed.
    assert "aaaa111" in str(ambiguous_branch.value)
    assert "aaaa222" in str(ambiguous_branch.value)

    with pytest.raises(ConfigError, match="RELEASE_SELECTOR_AMBIGUOUS"):
        resolve_release(store, "aaaa")


def test_an_unknown_selector_lists_what_is_actually_installed(tmp_path: Path) -> None:
    store = RuntimeReleaseStore(tmp_path)
    _install(store, "aaa1111", branch="main")

    with pytest.raises(ConfigError, match="RELEASE_NOT_FOUND") as unknown:
        resolve_release(store, "does-not-exist")
    assert "main" in str(unknown.value)


def test_selection_without_any_release_says_so(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="NO_RELEASES_INSTALLED"):
        resolve_release(RuntimeReleaseStore(tmp_path), "main")


def test_a_branchless_release_still_has_a_readable_label(tmp_path: Path) -> None:
    """Manifests written before `branch` existed must remain selectable and displayable."""
    store = RuntimeReleaseStore(tmp_path)
    _install(store, "aaa1111")

    choices = release_choices(store)
    assert [choice.label for choice in choices] == ["aaa1111"]
    assert resolve_release(store, "aaa1").commit_sha == "aaa1111"


def test_release_choices_mark_current_and_previous(tmp_path: Path) -> None:
    store = RuntimeReleaseStore(tmp_path)
    _install(store, "aaa1111", branch="main")
    _install(store, "bbb2222", branch="dev", built_at="2026-07-25T11:00:00+00:00")
    store.swap_current("aaa1111")
    store.swap_current("bbb2222")

    by_sha = {choice.commit_sha: choice for choice in release_choices(store)}
    assert by_sha["bbb2222"].is_current is True
    assert by_sha["aaa1111"].is_previous is True
    assert by_sha["bbb2222"].as_dict()["switch_command"] == "rf version switch dev"


def test_a_receipt_id_resolves_from_a_unique_prefix(tmp_path: Path) -> None:
    store = RuntimeReleaseStore(tmp_path)
    store.write_receipt(
        ActivationReceipt(
            receipt_id="act-20260725-001",
            from_sha=None,
            to_sha="aaa1111",
            to_fingerprint=_FINGERPRINT,
            tool_surface_hash=_SURFACE,
            rediscovery_required=False,
            outcome=ActivationOutcome.ACTIVATED,
            activated_at="2026-07-25T10:00:00+00:00",
            stage=ActivationStage.HEALTH_VERIFIED,
            observed_sha="aaa1111",
            converged=True,
        )
    )

    assert resolve_receipt_id(store, "act-20260725-001") == "act-20260725-001"
    assert resolve_receipt_id(store, "act-20260725-0") == "act-20260725-001"
    # An id this store has never seen is passed through: the store owns existence, and it
    # reports a missing or corrupt receipt with its own error rather than this resolver
    # inventing one.
    assert resolve_receipt_id(store, "act-19990101-001") == "act-19990101-001"


# --------------------------------------------------------------- inventory


def _observed(sha: str | None, phase: str = "healthy", pid: int | None = 4242) -> ObservedRuntime:
    return ObservedRuntime(running_release_sha=sha, phase=phase, pid=pid)


def _agent(*, registered: bool, loaded: bool) -> RegistrarStatus:
    return RegistrarStatus(
        registered=registered, loaded=loaded, detail="", unit_path="/tmp/agent.plist"
    )


def test_inventory_reports_the_live_release_by_label_and_how_to_switch(tmp_path: Path) -> None:
    store = RuntimeReleaseStore(tmp_path)
    _install(store, "aaa1111", branch="main", subject="ship it")
    _install(store, "bbb2222", branch="feat/x", built_at="2026-07-25T11:00:00+00:00")
    store.swap_current("aaa1111")
    store.swap_current("bbb2222")

    inventory = build_runtime_inventory(
        releases=release_choices(store),
        observed=_observed("bbb2222"),
        agent=_agent(registered=True, loaded=True),
        agent_secret_usable=True,
        dev_runtimes=[],
    )

    production = inventory["production"]
    assert isinstance(production, dict)
    assert production["active_label"] == "feat/x"
    assert production["converged"] is True
    assert production["phase"] == "healthy"
    assert production["pid"] == 4242
    assert production["os_resident"] is True
    assert production["durable_secret_usable"] is True
    rollback = inventory["rollback_target"]
    assert isinstance(rollback, dict)
    assert rollback["label"] == "main"
    assert rollback["switch_command"] == "rf version switch main"
    assert inventory["counts"] == {
        "releases": 2,
        "dev_runtimes": 0,
        "dev_runtimes_running": 0,
        # No process table was supplied, so nothing can be reported as unsupervised.
        "orphan_processes": 0,
        "orphan_processes_on_removed_releases": 0,
    }


def test_inventory_does_not_claim_convergence_when_observed_differs(tmp_path: Path) -> None:
    """Mid-activation the symlink and the live process disagree; the view must show both."""
    store = RuntimeReleaseStore(tmp_path)
    _install(store, "aaa1111", branch="main")
    _install(store, "bbb2222", branch="feat/x", built_at="2026-07-25T11:00:00+00:00")
    store.swap_current("bbb2222")

    inventory = build_runtime_inventory(
        releases=release_choices(store),
        observed=_observed("aaa1111"),
        agent=None,
        agent_secret_usable=False,
        dev_runtimes=[],
    )

    production = inventory["production"]
    assert isinstance(production, dict)
    assert production["active_label"] == "feat/x"
    assert production["observed_label"] == "main"
    assert production["converged"] is False
    # No launchd probe available must read as "not OS-resident", never as True.
    assert production["os_resident"] is False


def test_inventory_counts_running_dev_runtimes_and_names_them(tmp_path: Path) -> None:
    store = RuntimeReleaseStore(tmp_path)
    _install(store, "aaa1111", branch="main")
    store.swap_current("aaa1111")

    inventory = build_runtime_inventory(
        releases=release_choices(store),
        observed=_observed("aaa1111"),
        agent=_agent(registered=True, loaded=True),
        agent_secret_usable=True,
        dev_runtimes=[
            {"name": "feat-x", "phase": "healthy", "pid": 10},
            {"name": "feat-y", "phase": "stopped", "pid": None},
        ],
    )

    assert inventory["counts"] == {
        "releases": 1,
        "dev_runtimes": 2,
        "dev_runtimes_running": 1,
        "orphan_processes": 0,
        "orphan_processes_on_removed_releases": 0,
    }
    assert "feat-x" in str(inventory["safe_next_action"])


def test_inventory_on_an_empty_release_root_still_answers(tmp_path: Path) -> None:
    """This is the command someone runs when lost, so it must never need a healthy install."""
    inventory = build_runtime_inventory(
        releases=release_choices(RuntimeReleaseStore(tmp_path)),
        observed=None,
        agent=None,
        agent_secret_usable=False,
        dev_runtimes=[],
    )

    production = inventory["production"]
    assert isinstance(production, dict)
    assert production["active_label"] is None
    assert production["phase"] == "unknown"
    assert inventory["releases"] == []
    assert "rf upgrade --from-worktree" in str(inventory["safe_next_action"])


def test_a_listing_never_offers_an_ambiguous_switch_command(tmp_path: Path) -> None:
    """Deploying one branch twice is normal, and then its NAME resolves to two releases.

    Observed live: two `main` releases made `rf runtime ls` print
    `rf version switch main` for both, and running it failed with
    RELEASE_SELECTOR_AMBIGUOUS -- the listing was advertising a command that cannot work.
    Every offered selector must therefore resolve to exactly one release.
    """
    store = RuntimeReleaseStore(tmp_path)
    _install(store, "aaa1111", branch="main")
    _install(store, "bbb2222", branch="main", built_at="2026-07-25T11:00:00+00:00")
    _install(store, "ccc3333", branch="feat/solo", built_at="2026-07-25T12:00:00+00:00")

    choices = release_choices(store)
    by_sha = {choice.commit_sha: choice for choice in choices}

    # Shared branch -> the selector falls back to the short sha.
    assert by_sha["aaa1111"].selector == "aaa1111"
    assert by_sha["bbb2222"].selector == "bbb2222"
    # Unique branch -> the friendly name survives.
    assert by_sha["ccc3333"].selector == "feat/solo"

    # Every advertised command must actually resolve to the release it belongs to.
    for choice in choices:
        offered = str(choice.as_dict()["switch_command"])
        selector = offered.removeprefix("rf version switch ")
        assert resolve_release(store, selector).commit_sha == choice.commit_sha


def test_the_rollback_hint_is_also_unambiguous(tmp_path: Path) -> None:
    store = RuntimeReleaseStore(tmp_path)
    _install(store, "aaa1111", branch="main")
    _install(store, "bbb2222", branch="main", built_at="2026-07-25T11:00:00+00:00")
    store.swap_current("aaa1111")
    store.swap_current("bbb2222")

    inventory = build_runtime_inventory(
        releases=release_choices(store),
        observed=_observed("bbb2222"),
        agent=None,
        agent_secret_usable=False,
        dev_runtimes=[],
    )

    rollback = inventory["rollback_target"]
    assert isinstance(rollback, dict)
    selector = str(rollback["switch_command"]).removeprefix("rf version switch ")
    assert resolve_release(store, selector).commit_sha == "aaa1111"
