"""Coverage for issue #166: stale-workspace removal-safety evidence and the
guided orphan-cleanup nudge.

No automatic or TTL-based deletion exists anywhere in this module; removal
always stays an explicit, human/agent-invoked ``workspace_remove`` call, and a
candidate listing is advice only -- it never authorizes skipping
``workspace_remove``'s own real-time safety check.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import ForgeEnvironment, create_forge_environment

from repoforge.application.service import CodingService
from repoforge.bootstrap import AdapterOverrides, build_application
from repoforge.config import load_config
from repoforge.domain.errors import RepoForgeError
from repoforge.domain.workspace_removal import (
    PrLifecycleState,
    classify_removal_safety,
    order_candidates,
)
from repoforge.testing.fakes import FixedClock


def _set_stale_workspace_fields(
    env: ForgeEnvironment, *, threshold: int = 1, min_age_seconds: float = 0.0
) -> None:
    text = env.config_path.read_text(encoding="utf-8")
    assert "path_prefixes = " in text
    text = text.replace(
        "path_prefixes = ",
        f"stale_workspace_candidate_threshold = {threshold}\n"
        f"stale_workspace_min_age_seconds = {min_age_seconds}\n"
        "path_prefixes = ",
        1,
    )
    env.config_path.write_text(text, encoding="utf-8")


def _clocked_service(env: ForgeEnvironment, clock: FixedClock) -> CodingService:
    config = load_config(env.config_path)
    application = build_application(config, overrides=AdapterOverrides(clock=clock))
    return CodingService(config, application=application)


# ---------------------------------------------------------------------------
# Pure domain-level unit tests
# ---------------------------------------------------------------------------


def test_classify_safe_only_when_every_signal_is_known_and_clear() -> None:
    safe = classify_removal_safety(
        workspace_id="w1",
        clean=True,
        unpushed_commits=0,
        pr_state=PrLifecycleState.NONE,
        age_seconds=100.0,
    )
    assert safe.safe is True
    assert safe.blocking_reasons == ()


@pytest.mark.parametrize(
    ("clean", "unpushed", "pr_state", "expected_reason"),
    [
        (False, 0, PrLifecycleState.NONE, "dirty_tree"),
        (None, 0, PrLifecycleState.NONE, "unknown_tree_state"),
        (True, 3, PrLifecycleState.NONE, "unpushed_commits"),
        (True, None, PrLifecycleState.NONE, "unknown_push_state"),
        (True, 0, PrLifecycleState.OPEN, "open_pull_request"),
        (True, 0, PrLifecycleState.UNKNOWN, "unknown_pull_request_state"),
    ],
)
def test_classify_unsafe_for_each_blocking_condition(
    clean: bool | None, unpushed: int | None, pr_state: PrLifecycleState, expected_reason: str
) -> None:
    evidence = classify_removal_safety(
        workspace_id="w1",
        clean=clean,
        unpushed_commits=unpushed,
        pr_state=pr_state,
        age_seconds=1.0,
    )
    assert evidence.safe is False
    assert expected_reason in evidence.blocking_reasons


def test_merged_and_closed_pr_states_do_not_block_removal() -> None:
    for state in (PrLifecycleState.MERGED, PrLifecycleState.CLOSED, PrLifecycleState.NONE):
        evidence = classify_removal_safety(
            workspace_id="w1", clean=True, unpushed_commits=0, pr_state=state, age_seconds=1.0
        )
        assert evidence.safe is True


def test_order_candidates_drops_unsafe_and_orders_oldest_first() -> None:
    old = classify_removal_safety(
        workspace_id="old",
        clean=True,
        unpushed_commits=0,
        pr_state=PrLifecycleState.NONE,
        age_seconds=1000.0,
    )
    young = classify_removal_safety(
        workspace_id="young",
        clean=True,
        unpushed_commits=0,
        pr_state=PrLifecycleState.NONE,
        age_seconds=10.0,
    )
    unsafe = classify_removal_safety(
        workspace_id="unsafe",
        clean=False,
        unpushed_commits=0,
        pr_state=PrLifecycleState.NONE,
        age_seconds=5000.0,
    )
    ordered = order_candidates((young, unsafe, old))
    assert [item.workspace_id for item in ordered] == ["old", "young"]


# ---------------------------------------------------------------------------
# Real-workspace integration: removal-safety evidence
# ---------------------------------------------------------------------------


def test_clean_pushed_workspace_with_no_pr_is_a_safe_candidate(tmp_path: Path) -> None:
    from repoforge.application.workspace.removal_safety import compute_removal_safety

    env = create_forge_environment(tmp_path, require_verification=False)
    created = env.service.workspace_create("demo", "safe candidate")
    workspace_id = created["workspace_id"]
    env.service.workspace_push(workspace_id)

    record, _, _ = env.service.application.context.workspace(workspace_id)
    evidence = compute_removal_safety(env.service.application.context, record, check_pr_status=True)
    assert evidence.clean is True
    assert evidence.unpushed_commits == 0
    assert evidence.pr_state == "none"
    assert evidence.safe is True


def test_unpushed_commits_are_detected_and_block_safety(tmp_path: Path) -> None:
    from repoforge.application.workspace.removal_safety import compute_removal_safety

    env = create_forge_environment(tmp_path, require_verification=False)
    created = env.service.workspace_create("demo", "unpushed commits")
    workspace_id = created["workspace_id"]
    workspace_path = Path(created["path"])
    (workspace_path / "scratch.txt").write_text("x\n", encoding="utf-8")
    from conftest import git as _git  # local helper reuse

    _git("add", "scratch.txt", cwd=workspace_path)
    _git("commit", "-m", "local only", cwd=workspace_path)

    record, _, _ = env.service.application.context.workspace(workspace_id)
    evidence = compute_removal_safety(
        env.service.application.context, record, check_pr_status=False
    )
    assert evidence.unpushed_commits == 1
    assert evidence.safe is False
    assert "unpushed_commits" in evidence.blocking_reasons


def test_dirty_workspace_is_never_a_safe_candidate(tmp_path: Path) -> None:
    from repoforge.application.workspace.removal_safety import compute_removal_safety

    env = create_forge_environment(tmp_path, require_verification=False)
    created = env.service.workspace_create("demo", "dirty workspace")
    workspace_id = created["workspace_id"]
    Path(created["path"], "scratch.txt").write_text("uncommitted\n", encoding="utf-8")

    record, _, _ = env.service.application.context.workspace(workspace_id)
    evidence = compute_removal_safety(
        env.service.application.context, record, check_pr_status=False
    )
    assert evidence.clean is False
    assert evidence.safe is False


# ---------------------------------------------------------------------------
# Nudge wiring: workspace_create / workspace_list
# ---------------------------------------------------------------------------


def test_stale_workspaces_nudge_appears_once_threshold_met_and_respects_rate_limit(
    tmp_path: Path,
) -> None:
    env = create_forge_environment(tmp_path, require_verification=False)
    _set_stale_workspace_fields(env, threshold=2, min_age_seconds=0.0)
    clock = FixedClock("2026-01-01T00:00:00+00:00")
    service = _clocked_service(env, clock)

    first = service.workspace_create("demo", "candidate one")
    service.workspace_push(first["workspace_id"])
    assert first["stale_workspaces"] is None  # only one safe candidate so far

    second = service.workspace_create("demo", "candidate two")
    service.workspace_push(second["workspace_id"])
    # Now two safe, pushed, PR-less workspaces exist -- threshold (2) is met.
    nudge = second["stale_workspaces"]
    assert nudge is not None
    assert nudge["count"] >= 2
    assert len(nudge["candidates"]) <= 5
    assert "workspace_remove" in nudge["safe_next_action"]

    # Rate limit: an immediate follow-up workspace_list call must not repeat it.
    listed = service.workspace_list()
    assert listed["stale_workspaces"] is None


def test_workspaces_younger_than_min_age_are_not_candidates(tmp_path: Path) -> None:
    env = create_forge_environment(tmp_path, require_verification=False)
    _set_stale_workspace_fields(env, threshold=1, min_age_seconds=3_600.0)
    clock = FixedClock("2026-01-01T00:00:00+00:00")
    service = _clocked_service(env, clock)

    created = service.workspace_create("demo", "too young")
    service.workspace_push(created["workspace_id"])
    assert created["stale_workspaces"] is None

    listed = service.workspace_list()
    assert listed["stale_workspaces"] is None


# ---------------------------------------------------------------------------
# workspace_remove safety envelope
# ---------------------------------------------------------------------------


def test_workspace_remove_refuses_dirty_tree_with_actionable_guidance(
    forge_env: ForgeEnvironment,
) -> None:
    created = forge_env.service.workspace_create("demo", "remove dirty refusal")
    workspace_id = created["workspace_id"]
    Path(created["path"], "scratch.txt").write_text("uncommitted\n", encoding="utf-8")

    with pytest.raises(RepoForgeError) as exc:
        forge_env.service.workspace_remove(workspace_id)
    assert "clean" in str(exc.value).lower()
    assert "workspace_restore_paths" in (exc.value.safe_next_action or "")


def test_workspace_remove_refuses_unpushed_commits_with_actionable_guidance(
    tmp_path: Path,
) -> None:
    env = create_forge_environment(tmp_path, require_verification=False)
    created = env.service.workspace_create("demo", "remove unpushed refusal")
    workspace_id = created["workspace_id"]
    workspace_path = Path(created["path"])
    (workspace_path / "scratch.txt").write_text("x\n", encoding="utf-8")
    from conftest import git as _git

    _git("add", "scratch.txt", cwd=workspace_path)
    _git("commit", "-m", "local only", cwd=workspace_path)

    with pytest.raises(RepoForgeError) as exc:
        env.service.workspace_remove(workspace_id)
    assert "not pushed" in str(exc.value).lower()
    assert "workspace_push" in (exc.value.safe_next_action or "")


def test_workspace_remove_succeeds_for_a_genuinely_clean_pushed_workspace(
    forge_env: ForgeEnvironment,
) -> None:
    created = forge_env.service.workspace_create("demo", "remove clean success")
    workspace_id = created["workspace_id"]
    result = forge_env.service.workspace_remove(workspace_id)
    assert result["removed"] is True


# ---------------------------------------------------------------------------
# doctor section
# ---------------------------------------------------------------------------


def test_doctor_reports_workspace_count_disk_usage_and_removable_candidates(
    tmp_path: Path,
) -> None:
    env = create_forge_environment(tmp_path, require_verification=False)
    created = env.service.workspace_create("demo", "doctor coverage")
    env.service.workspace_push(created["workspace_id"])

    report = env.service.doctor()
    workspaces = report["workspaces"]
    assert workspaces["count"] == 1
    assert workspaces["existing_on_disk"] == 1
    assert workspaces["disk_usage_bytes"] > 0
    assert any(
        item["workspace_id"] == created["workspace_id"]
        for item in workspaces["removable_candidates"]
    )
