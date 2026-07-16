from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from conftest import ForgeEnvironment, create_forge_environment
from test_ci_failure_evidence import _failed_state, _publish_workspace, _update_state

from repoforge.application.nudges import _MAX_TRACKED_KEYS, AdoptionNudgeTracker


class _MutableClock:
    """A deterministic, manually-advanced clock for exercising nudge time windows."""

    def __init__(self, start_epoch: float) -> None:
        self._epoch = start_epoch

    def now_iso(self) -> str:
        return datetime.fromtimestamp(self._epoch, tz=timezone.utc).isoformat()

    def advance(self, seconds: float) -> None:
        self._epoch += seconds


def _pending_checks_state() -> dict[str, object]:
    return {
        "checks": [
            {
                "name": "integration",
                "state": "IN_PROGRESS",
                "bucket": "pending",
                "link": "https://github.com/owner/demo/actions/runs/5001/job/5001",
                "workflow": "CI",
                "description": "running",
                "startedAt": "2026-07-15T00:00:00Z",
                "completedAt": "",
            }
        ]
    }


def _resolved_checks_state() -> dict[str, object]:
    return {
        "checks": [
            {
                "name": "integration",
                "state": "SUCCESS",
                "bucket": "pass",
                "link": "https://github.com/owner/demo/actions/runs/5001/job/5001",
                "workflow": "CI",
                "description": "ok",
                "startedAt": "2026-07-15T00:00:00Z",
                "completedAt": "2026-07-15T00:05:00Z",
            }
        ]
    }


# --- Nudge 1: repeated pending workspace_pr_checks polling -> workspace_pr_watch ---


def test_pr_checks_repeated_pending_poll_nudges_exactly_at_third_call(
    forge_env: ForgeEnvironment,
) -> None:
    workspace_id, _ = _publish_workspace(forge_env)
    _update_state(forge_env, _pending_checks_state())

    first = forge_env.service.workspace_pr_checks(workspace_id)
    assert first["pending"] is True
    assert "workspace_pr_watch" not in first["next_step"]

    second = forge_env.service.workspace_pr_checks(workspace_id)
    assert "workspace_pr_watch" not in second["next_step"]

    third = forge_env.service.workspace_pr_checks(workspace_id)
    assert f'workspace_pr_watch(workspace_id="{workspace_id}")' in third["next_step"]


def test_pr_checks_repeated_poll_nudge_is_scoped_to_its_own_workspace(
    forge_env: ForgeEnvironment,
) -> None:
    workspace_one, _ = _publish_workspace(forge_env)
    workspace_two, _ = _publish_workspace(forge_env)
    _update_state(forge_env, _pending_checks_state())

    for _ in range(2):
        forge_env.service.workspace_pr_checks(workspace_one)
    third = forge_env.service.workspace_pr_checks(workspace_one)
    assert "workspace_pr_watch" in third["next_step"]

    first_other = forge_env.service.workspace_pr_checks(workspace_two)
    assert "workspace_pr_watch" not in first_other["next_step"]


def test_pr_checks_poll_nudge_resets_once_checks_resolve(forge_env: ForgeEnvironment) -> None:
    workspace_id, _ = _publish_workspace(forge_env)
    _update_state(forge_env, _pending_checks_state())

    result = None
    for _ in range(3):
        result = forge_env.service.workspace_pr_checks(workspace_id)
    assert result is not None
    assert "workspace_pr_watch" in result["next_step"]

    _update_state(forge_env, _resolved_checks_state())
    resolved = forge_env.service.workspace_pr_checks(workspace_id)
    assert resolved["pending"] is False
    assert "workspace_pr_watch" not in resolved["next_step"]

    _update_state(forge_env, _pending_checks_state())
    first_after_reset = forge_env.service.workspace_pr_checks(workspace_id)
    assert "workspace_pr_watch" not in first_after_reset["next_step"]


def test_pr_checks_poll_nudge_only_counts_polls_within_the_ten_minute_window(
    tmp_path: Path,
) -> None:
    clock = _MutableClock(1_700_000_000.0)
    env = create_forge_environment(tmp_path, clock=clock)
    workspace_id, _ = _publish_workspace(env)
    _update_state(env, _pending_checks_state())

    env.service.workspace_pr_checks(workspace_id)  # t=0
    clock.advance(400.0)
    env.service.workspace_pr_checks(workspace_id)  # t=400
    clock.advance(400.0)
    third = env.service.workspace_pr_checks(workspace_id)  # t=800; t=0 poll now falls outside 600s
    assert "workspace_pr_watch" not in third["next_step"]

    clock.advance(1.0)
    fourth = env.service.workspace_pr_checks(workspace_id)  # t=801; t=400,800,801 all in-window
    assert "workspace_pr_watch" in fourth["next_step"]


# --- Nudge 3: failing required check -> workspace_pr_check_details / failure_evidence ---


def test_pr_checks_failing_required_check_nudges_immediately_with_a_selector_that_works(
    forge_env: ForgeEnvironment,
) -> None:
    workspace_id, head_sha = _publish_workspace(forge_env)
    _update_state(forge_env, _failed_state(head_sha))

    result = forge_env.service.workspace_pr_checks(workspace_id)
    selector = result["checks"][0]["selector"]
    assert selector == "check-run:7001"
    assert "workspace_pr_check_details" in result["next_step"]
    assert "workspace_pr_failure_evidence" in result["next_step"]
    assert f'check_selector="{selector}"' in result["next_step"]
    assert workspace_id in result["next_step"]

    # The nudge must not just look plausible: the selector it names must actually work.
    details = forge_env.service.workspace_pr_check_details(workspace_id, selector)
    assert details["check_run_id"] == 7001
    evidence = forge_env.service.workspace_pr_failure_evidence(workspace_id, selector)
    assert evidence["check_run_id"] == 7001


def test_pr_checks_optional_failing_check_does_not_nudge_failure_evidence(
    forge_env: ForgeEnvironment,
) -> None:
    workspace_id, head_sha = _publish_workspace(forge_env)
    state = _failed_state(head_sha)
    state["checks"][0]["required"] = False  # type: ignore[index]
    _update_state(forge_env, state)

    result = forge_env.service.workspace_pr_checks(workspace_id)
    assert result["summary"].get("fail", 0) >= 1
    assert "workspace_pr_check_details" not in result["next_step"]
    assert "workspace_pr_failure_evidence" not in result["next_step"]


def test_pr_checks_all_green_result_carries_no_nudge(forge_env: ForgeEnvironment) -> None:
    workspace_id, _ = _publish_workspace(forge_env)

    result = forge_env.service.workspace_pr_checks(workspace_id)
    assert result["all_passed"] is True
    assert "workspace_pr_watch" not in result["next_step"]
    assert "workspace_pr_check_details" not in result["next_step"]
    assert "workspace_pr_failure_evidence" not in result["next_step"]


# --- Nudge 2: repeated single-file workspace_read_file -> workspace_read_files ---


def test_read_file_nudges_exactly_at_fifth_consecutive_single_read(
    forge_env: ForgeEnvironment,
) -> None:
    workspace_id = forge_env.service.workspace_create("demo", "read nudge")["workspace_id"]

    for _ in range(4):
        read = forge_env.service.workspace_read_file(workspace_id, "hello.txt")
        assert "workspace_read_files" not in read["next_step"]

    fifth = forge_env.service.workspace_read_file(workspace_id, "hello.txt")
    assert f'workspace_read_files(workspace_id="{workspace_id}"' in fifth["next_step"]


def test_read_file_nudge_is_scoped_to_its_own_workspace(forge_env: ForgeEnvironment) -> None:
    workspace_one = forge_env.service.workspace_create("demo", "read nudge one")["workspace_id"]
    workspace_two = forge_env.service.workspace_create("demo", "read nudge two")["workspace_id"]

    for _ in range(4):
        forge_env.service.workspace_read_file(workspace_one, "hello.txt")
    fifth = forge_env.service.workspace_read_file(workspace_one, "hello.txt")
    assert "workspace_read_files" in fifth["next_step"]

    first_other = forge_env.service.workspace_read_file(workspace_two, "hello.txt")
    assert "workspace_read_files" not in first_other["next_step"]


def test_read_files_batch_resets_the_single_read_nudge_counter(
    forge_env: ForgeEnvironment,
) -> None:
    workspace_id = forge_env.service.workspace_create("demo", "read nudge batch")["workspace_id"]

    for _ in range(4):
        forge_env.service.workspace_read_file(workspace_id, "hello.txt")

    batch = forge_env.service.workspace_read_files(workspace_id, ["hello.txt", "README.md"])
    assert all("next_step" not in entry for entry in batch["files"])

    first_after_batch = forge_env.service.workspace_read_file(workspace_id, "hello.txt")
    assert "workspace_read_files" not in first_after_batch["next_step"]


def test_read_file_nudge_does_not_fire_when_batching_is_used_instead(
    forge_env: ForgeEnvironment,
) -> None:
    workspace_id = forge_env.service.workspace_create("demo", "read nudge instead")[
        "workspace_id"
    ]

    for _ in range(3):
        forge_env.service.workspace_read_files(workspace_id, ["hello.txt", "README.md"])

    read = forge_env.service.workspace_read_file(workspace_id, "hello.txt")
    assert "workspace_read_files" not in read["next_step"]


# --- Session-local tracking: bounded, in-memory only, never audited ---


def test_tracker_is_bounded_across_many_distinct_workspaces() -> None:
    tracker = AdoptionNudgeTracker()
    now = 1_000_000.0
    for index in range(_MAX_TRACKED_KEYS * 3):
        tracker.observe_pending_pr_check_poll(f"workspace-{index}", now)
        tracker.observe_single_file_read(f"workspace-{index}", now)
        now += 0.01

    assert len(tracker._pr_check_polls._data) <= _MAX_TRACKED_KEYS
    assert len(tracker._file_reads._data) <= _MAX_TRACKED_KEYS


def test_tracker_history_per_workspace_is_bounded_to_its_threshold() -> None:
    tracker = AdoptionNudgeTracker()
    now = 1_000_000.0
    for _ in range(50):
        tracker.observe_single_file_read("workspace-a", now)
        now += 0.01

    assert (
        len(tracker._file_reads._data["workspace-a"])
        <= AdoptionNudgeTracker.FILE_READ_THRESHOLD
    )


def test_nudge_details_never_appear_in_audit_payloads(forge_env: ForgeEnvironment) -> None:
    workspace_id, _ = _publish_workspace(forge_env)
    _update_state(forge_env, _pending_checks_state())
    for _ in range(3):
        forge_env.service.workspace_pr_checks(workspace_id)
    forge_env.service.workspace_read_file(workspace_id, "hello.txt")

    audit_path = forge_env.root / "state" / "audit.jsonl"
    events = [
        json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines() if line
    ]

    pr_checks_events = [event for event in events if event["action"] == "workspace_pr_checks"]
    read_events = [event for event in events if event["action"] == "workspace_read_file"]
    assert pr_checks_events and read_events

    for event in pr_checks_events:
        assert set(event["details"]) <= {
            "workspace_id",
            "required_only",
            "correlation_id",
            "duration_ms",
            "result_bytes",
        }
    for event in read_events:
        assert set(event["details"]) <= {
            "workspace_id",
            "path",
            "correlation_id",
            "duration_ms",
            "result_bytes",
        }
