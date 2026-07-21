from __future__ import annotations

import json
from pathlib import Path

import pytest

from repoforge.adapters.audit import JsonlAuditSink
from repoforge.adapters.audit.query import read_audit_events, summarize_operation_metrics
from repoforge.adapters.observability import JsonMetricsSink
from repoforge.domain.errors import ConfigError
from repoforge.testing import FixedClock, InMemoryLockManager


def test_read_audit_events_orders_newest_first_and_bounds_limit(tmp_path: Path) -> None:
    clock = FixedClock("2026-07-15T00:00:00+00:00")
    sink = JsonlAuditSink(tmp_path, clock)
    sink.record("workspace_create", success=True, details={"duration_ms": 1.0})
    sink.record("workspace_status", success=True, details={"duration_ms": 2.0})
    sink.record("workspace_commit", success=True, details={"duration_ms": 3.0})

    events = read_audit_events(sink.path, limit=2)
    assert [event["action"] for event in events] == ["workspace_commit", "workspace_status"]


def test_read_audit_events_filters_by_action(tmp_path: Path) -> None:
    clock = FixedClock("2026-07-15T00:00:00+00:00")
    sink = JsonlAuditSink(tmp_path, clock)
    sink.record("workspace_create", success=True, details={"duration_ms": 1.0})
    sink.record("workspace_status", success=True, details={"duration_ms": 2.0})

    events = read_audit_events(sink.path, limit=10, action="workspace_status")
    assert len(events) == 1
    assert events[0]["action"] == "workspace_status"


def test_read_audit_events_filters_failed_only(tmp_path: Path) -> None:
    clock = FixedClock("2026-07-15T00:00:00+00:00")
    sink = JsonlAuditSink(tmp_path, clock)
    sink.record("workspace_verify", success=True, details={"duration_ms": 1.0})
    sink.record(
        "workspace_verify",
        success=False,
        details={"duration_ms": 2.0, "error_code": "COMMAND_FAILED"},
    )

    events = read_audit_events(sink.path, limit=10, only_failed=True)
    assert len(events) == 1
    assert events[0]["success"] is False
    assert events[0]["details"]["error_code"] == "COMMAND_FAILED"


def test_read_audit_events_filters_by_min_duration(tmp_path: Path) -> None:
    clock = FixedClock("2026-07-15T00:00:00+00:00")
    sink = JsonlAuditSink(tmp_path, clock)
    sink.record("workspace_run_profile", success=True, details={"duration_ms": 100.0})
    sink.record("workspace_run_profile", success=True, details={"duration_ms": 5_000.0})

    events = read_audit_events(sink.path, limit=10, min_duration_ms=1_000.0)
    assert len(events) == 1
    assert events[0]["details"]["duration_ms"] == 5_000.0


def test_read_audit_events_filters_by_min_bytes(tmp_path: Path) -> None:
    clock = FixedClock("2026-07-15T00:00:00+00:00")
    sink = JsonlAuditSink(tmp_path, clock)
    sink.record("workspace_diff", success=True, details={"duration_ms": 1.0, "result_bytes": 100})
    sink.record(
        "workspace_status", success=True, details={"duration_ms": 1.0, "result_bytes": 50_000}
    )

    events = read_audit_events(sink.path, limit=10, min_bytes=1_000.0)
    assert len(events) == 1
    assert events[0]["action"] == "workspace_status"
    assert events[0]["details"]["result_bytes"] == 50_000


def test_read_audit_events_min_bytes_skips_events_missing_result_bytes(tmp_path: Path) -> None:
    clock = FixedClock("2026-07-15T00:00:00+00:00")
    sink = JsonlAuditSink(tmp_path, clock)
    # A failure event, or a legacy success event, has no result_bytes at all.
    sink.record("workspace_verify", success=False, details={"duration_ms": 1.0})

    events = read_audit_events(sink.path, limit=10, min_bytes=0.0)
    assert events == []


def test_read_audit_events_rejects_out_of_bound_limit(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="between 1 and 1000"):
        read_audit_events(tmp_path / "audit.jsonl", limit=0)
    with pytest.raises(ConfigError, match="between 1 and 1000"):
        read_audit_events(tmp_path / "audit.jsonl", limit=1001)


def test_read_audit_events_missing_file_returns_empty(tmp_path: Path) -> None:
    assert read_audit_events(tmp_path / "missing-audit.jsonl", limit=10) == []


def test_read_audit_events_skips_malformed_lines(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    path.write_text(
        '{"action": "ok", "success": true, "details": {}}\nnot-json\n[1, 2, 3]\n',
        encoding="utf-8",
    )
    events = read_audit_events(path, limit=10)
    assert len(events) == 1
    assert events[0]["action"] == "ok"


def test_summarize_operation_metrics_aggregates_and_sorts_by_avg_duration() -> None:
    snapshot = {
        "version": 1,
        "operations": {
            "workspace_run_profile": {
                "count": 4,
                "successes": 2,
                "failures": 2,
                "duration_ms_total": 4_000.0,
                "duration_ms_max": 3_000.0,
                "failure_categories": {"COMMAND_FAILED": 2},
            },
            "workspace_status": {
                "count": 10,
                "successes": 10,
                "failures": 0,
                "duration_ms_total": 100.0,
                "duration_ms_max": 20.0,
                "failure_categories": {},
            },
        },
    }
    rows = summarize_operation_metrics(snapshot)
    assert [row["action"] for row in rows] == ["workspace_run_profile", "workspace_status"]
    slow = rows[0]
    assert slow["count"] == 4
    assert slow["failures"] == 2
    assert slow["failure_rate"] == 0.5
    assert slow["duration_ms_avg"] == 1_000.0
    assert slow["duration_ms_max"] == 3_000.0
    assert slow["top_error_codes"] == [["COMMAND_FAILED", 2]]


def test_summarize_operation_metrics_handles_empty_snapshot() -> None:
    assert summarize_operation_metrics({"version": 1, "operations": {}}) == []
    assert summarize_operation_metrics({}) == []


def test_summarize_operation_metrics_matches_real_metrics_sink(tmp_path: Path) -> None:
    locks = InMemoryLockManager()
    metrics = JsonMetricsSink(tmp_path, locks)
    metrics.record("workspace_commit", success=True, duration_ms=42.0, error_code=None)
    metrics.record("workspace_commit", success=False, duration_ms=8.0, error_code="STALE_STATE")

    rows = summarize_operation_metrics(metrics.snapshot())
    assert len(rows) == 1
    assert rows[0]["action"] == "workspace_commit"
    assert rows[0]["count"] == 2
    assert rows[0]["failures"] == 1
    assert rows[0]["top_error_codes"] == [["STALE_STATE", 1]]


def test_result_bytes_average_uses_only_observed_successful_payloads(tmp_path: Path) -> None:
    metrics = JsonMetricsSink(tmp_path, InMemoryLockManager())
    metrics.record(
        "workspace_diff",
        success=True,
        duration_ms=1.0,
        error_code=None,
        result_bytes=10_000,
    )
    for _ in range(9):
        metrics.record(
            "workspace_diff",
            success=False,
            duration_ms=1.0,
            error_code="COMMAND_FAILED",
        )

    snapshot = metrics.snapshot()
    stats = snapshot["operations"]["workspace_diff"]
    assert stats["count"] == 10
    assert stats["result_bytes_count"] == 1
    assert summarize_operation_metrics(snapshot)[0]["result_bytes_avg"] == 10_000.0


def test_summarize_operation_metrics_since_aggregates_only_matching_day_buckets(
    tmp_path: Path,
) -> None:
    locks = InMemoryLockManager()
    clock = FixedClock("2026-07-13T00:00:00+00:00")
    metrics = JsonMetricsSink(tmp_path, locks, clock)
    metrics.record("workspace_commit", success=True, duration_ms=100.0, error_code=None)

    clock.value = "2026-07-14T00:00:00+00:00"
    metrics.record("workspace_commit", success=True, duration_ms=10.0, error_code=None)
    metrics.record("workspace_commit", success=False, duration_ms=30.0, error_code="STALE_STATE")

    clock.value = "2026-07-15T00:00:00+00:00"
    metrics.record("workspace_commit", success=True, duration_ms=1_000.0, error_code=None)

    # Window covers only 07-14: the 07-13 and 07-15 calls must not be counted.
    rows = summarize_operation_metrics(metrics.snapshot(), since="2026-07-14", until="2026-07-14")
    assert len(rows) == 1
    row = rows[0]
    assert row["action"] == "workspace_commit"
    assert row["count"] == 2
    assert row["failures"] == 1
    assert row["failure_rate"] == 0.5
    assert row["duration_ms_avg"] == 20.0
    assert row["duration_ms_max"] == 30.0
    assert row["top_error_codes"] == [["STALE_STATE", 1]]

    # Lifetime totals (no since/until) still include all three days, unchanged.
    lifetime_rows = summarize_operation_metrics(metrics.snapshot())
    assert lifetime_rows[0]["count"] == 4


def test_summarize_operation_metrics_since_only_is_open_ended(tmp_path: Path) -> None:
    locks = InMemoryLockManager()
    clock = FixedClock("2026-07-13T00:00:00+00:00")
    metrics = JsonMetricsSink(tmp_path, locks, clock)
    metrics.record("workspace_status", success=True, duration_ms=1.0, error_code=None)
    clock.value = "2026-07-20T00:00:00+00:00"
    metrics.record("workspace_status", success=True, duration_ms=1.0, error_code=None)

    rows = summarize_operation_metrics(metrics.snapshot(), since="2026-07-14")
    assert rows[0]["count"] == 1


def test_summarize_operation_metrics_rejects_malformed_or_inverted_window() -> None:
    snapshot = {"version": 2, "operations": {}, "buckets": {}}
    with pytest.raises(ConfigError, match="Invalid --since date"):
        summarize_operation_metrics(snapshot, since="not-a-date")
    with pytest.raises(ConfigError, match="Invalid --until date"):
        summarize_operation_metrics(snapshot, since="2026-07-01", until="not-a-date")
    with pytest.raises(ConfigError, match="must not be after"):
        summarize_operation_metrics(snapshot, since="2026-07-10", until="2026-07-01")


def test_summarize_operation_metrics_since_tolerates_corrupt_bucket_entries() -> None:
    snapshot = {
        "version": 2,
        "operations": {},
        "buckets": {
            "not-a-date": {"workspace_status": {"count": 99}},
            "2026-07-14": "not-a-dict",
            "2026-07-15": {
                "workspace_status": "not-a-dict",
                "workspace_commit": {
                    "count": 1,
                    "successes": 1,
                    "failures": 0,
                    "duration_ms_total": 5.0,
                    "duration_ms_max": 5.0,
                    "failure_categories": {},
                },
            },
        },
    }
    rows = summarize_operation_metrics(snapshot, since="2026-07-01", until="2026-07-31")
    assert len(rows) == 1
    assert rows[0]["action"] == "workspace_commit"
    assert rows[0]["count"] == 1


def test_summarize_operation_metrics_since_matches_manual_jsonl_aggregation(
    tmp_path: Path,
) -> None:
    """Acceptance criterion: `rf audit stats --since` matches hand-aggregated JSONL fixture data."""
    locks = InMemoryLockManager()
    clock = FixedClock("2026-07-13T00:00:00+00:00")
    audit = JsonlAuditSink(tmp_path, clock)
    metrics = JsonMetricsSink(tmp_path, locks, clock)

    def _call(action: str, *, success: bool, duration_ms: float, error_code: str | None) -> None:
        details: dict[str, object] = {"duration_ms": duration_ms}
        if not success and error_code:
            details["error_code"] = error_code
        audit.record(action, success=success, details=details)
        metrics.record(action, success=success, duration_ms=duration_ms, error_code=error_code)

    _call("workspace_apply_patch", success=True, duration_ms=50.0, error_code=None)
    clock.value = "2026-07-14T00:00:00+00:00"
    _call("workspace_apply_patch", success=False, duration_ms=200.0, error_code="PATCH_REJECTED")
    _call("workspace_apply_patch", success=True, duration_ms=100.0, error_code=None)
    _call("workspace_status", success=True, duration_ms=2.0, error_code=None)
    clock.value = "2026-07-16T00:00:00+00:00"
    _call("workspace_apply_patch", success=True, duration_ms=999.0, error_code=None)

    since, until = "2026-07-14", "2026-07-14"

    # Manually aggregate the fixture JSONL for the same window, independent of the sink.
    manual: dict[str, dict[str, object]] = {}
    for line in audit.path.read_text(encoding="utf-8").splitlines():
        event = json.loads(line)
        day = event["timestamp"][:10]
        if not (since <= day <= until):
            continue
        action = event["action"]
        bucket = manual.setdefault(
            action, {"count": 0, "failures": 0, "duration_total": 0.0, "duration_max": 0.0}
        )
        bucket["count"] += 1
        duration = event["details"]["duration_ms"]
        bucket["duration_total"] += duration
        bucket["duration_max"] = max(bucket["duration_max"], duration)
        if not event["success"]:
            bucket["failures"] += 1

    rows = {
        row["action"]: row
        for row in summarize_operation_metrics(metrics.snapshot(), since=since, until=until)
    }
    assert set(rows) == set(manual)
    for action, expected in manual.items():
        actual = rows[action]
        assert actual["count"] == expected["count"]
        assert actual["failures"] == expected["failures"]
        assert actual["duration_ms_avg"] == round(expected["duration_total"] / expected["count"], 3)
        assert actual["duration_ms_max"] == expected["duration_max"]


def test_metrics_file_without_result_bytes_fields_loads_and_accumulates_and_since_still_works(
    tmp_path: Path,
) -> None:
    """Migration: a metrics file recorded before result_bytes existed loads compatibly
    (missing fields default to 0), keeps accumulating new result_bytes correctly, and
    `rf audit stats --since`/`--until` continues to work over the mixed data."""
    path = tmp_path / "operation-metrics.json"
    tmp_path.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "version": 2,
                "operations": {
                    "workspace_status": {
                        "count": 3,
                        "successes": 3,
                        "failures": 0,
                        "duration_ms_total": 30.0,
                        "duration_ms_max": 15.0,
                        "failure_categories": {},
                    }
                },
                "buckets": {
                    "2026-07-13": {
                        "workspace_status": {
                            "count": 1,
                            "successes": 1,
                            "failures": 0,
                            "duration_ms_total": 5.0,
                            "duration_ms_max": 5.0,
                            "failure_categories": {},
                        }
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    locks = InMemoryLockManager()
    clock = FixedClock("2026-07-13T00:00:00+00:00")
    metrics = JsonMetricsSink(tmp_path, locks, clock)

    # Legacy entries (no result_bytes fields at all) degrade to 0, never crash.
    lifetime_rows = summarize_operation_metrics(metrics.snapshot())
    assert lifetime_rows[0]["count"] == 3
    assert lifetime_rows[0]["result_bytes_avg"] == 0.0
    assert lifetime_rows[0]["result_bytes_max"] == 0

    metrics.record(
        "workspace_status", success=True, duration_ms=1.0, error_code=None, result_bytes=200
    )
    snapshot = metrics.snapshot()
    lifetime = snapshot["operations"]["workspace_status"]
    assert lifetime["count"] == 4
    assert lifetime["result_bytes_total"] == 200
    assert lifetime["result_bytes_max"] == 200
    assert lifetime["result_bytes_count"] == 1

    windowed_rows = summarize_operation_metrics(snapshot, since="2026-07-13", until="2026-07-13")
    assert windowed_rows[0]["count"] == 2
    assert windowed_rows[0]["result_bytes_avg"] == 200.0
    assert windowed_rows[0]["result_bytes_max"] == 200


class _CompactionClock:
    def __init__(self) -> None:
        self._tick = 0

    def now_iso(self) -> str:
        self._tick += 1
        return f"2026-07-21T08:{self._tick // 60:02d}:{self._tick % 60:02d}+00:00"


def _compaction_details(index: int) -> dict[str, object]:
    return {
        "correlation_id": f"corr-{index}",
        "correlation_hash": f"hashed-corr-{index}",
        "duration_ms": float(index),
        "result_bytes": 1024,
        "is_mutating": False,
        "origin": "model",
        "session_hash": "4f8c1a4f8c1a4f8c1a4f8c1a",
        "repo_count": 4,
        "selection_outcome": "exact_match",
        "repo_id": "repoforge",
    }


def test_identical_repo_list_successes_are_compacted_by_at_least_95_percent(
    tmp_path: Path,
) -> None:
    sink = JsonlAuditSink(tmp_path, _CompactionClock())

    for index in range(200):
        sink.record("repo_list", success=True, details=_compaction_details(index))

    events = [json.loads(line) for line in sink.path.read_text(encoding="utf-8").splitlines()]
    assert len(events) <= 10
    summaries = [event for event in events if event["details"].get("audit_summary") is True]
    assert summaries
    assert sum(int(event["details"]["suppressed_count"]) for event in summaries) >= 175
    assert all(event["details"]["origin"] == "model" for event in summaries)
    assert all(
        event["details"]["session_hash"] == "4f8c1a4f8c1a4f8c1a4f8c1a" for event in summaries
    )
    assert all("correlation_id" not in event["details"] for event in summaries)
    assert all("correlation_hash" not in event["details"] for event in summaries)


def test_repo_list_failures_and_state_changes_are_never_suppressed(tmp_path: Path) -> None:
    sink = JsonlAuditSink(tmp_path, _CompactionClock())
    for index in range(60):
        sink.record("repo_list", success=True, details=_compaction_details(index))

    changed = _compaction_details(61)
    changed["selection_outcome"] = "input_required"
    sink.record("repo_list", success=True, details=changed)

    failed = _compaction_details(62)
    failed["error_code"] = "SECURITY_POLICY_VIOLATION"
    sink.record("repo_list", success=False, details=failed)

    events = [json.loads(line) for line in sink.path.read_text(encoding="utf-8").splitlines()]
    failures = [event for event in events if event["success"] is False]
    assert len(failures) == 1
    assert failures[0]["details"]["error_code"] == "SECURITY_POLICY_VIOLATION"
    assert any(
        event["details"].get("selection_outcome") == "input_required"
        and event["details"].get("audit_summary") is not True
        for event in events
    )


def test_repo_list_compaction_summaries_survive_rotation_and_bounded_reads(
    tmp_path: Path,
) -> None:
    sink = JsonlAuditSink(
        tmp_path,
        _CompactionClock(),
        max_bytes=1_400,
        backup_count=2,
    )
    for index in range(200):
        sink.record("repo_list", success=True, details=_compaction_details(index))

    assert sink.path.with_suffix(".jsonl.1").is_file()
    events = read_audit_events(sink.path, limit=200, action="repo_list")
    assert events
    assert any(event["details"].get("audit_summary") is True for event in events)
    assert all(event["details"].get("origin") == "model" for event in events)


def test_operation_metrics_are_segmented_by_origin(tmp_path: Path) -> None:
    metrics = JsonMetricsSink(
        tmp_path,
        InMemoryLockManager(),
        FixedClock("2026-07-21T00:00:00+00:00"),
    )

    metrics.record(
        "repo_list",
        success=True,
        duration_ms=1.0,
        error_code=None,
        origin="model",
    )
    metrics.record(
        "repo_list",
        success=True,
        duration_ms=2.0,
        error_code=None,
        origin="connector",
    )
    metrics.record(
        "repo_list",
        success=False,
        duration_ms=3.0,
        error_code="COMMAND_FAILED",
        origin="model",
    )

    assert metrics.snapshot()["calls_by_origin"] == {
        "repo_list": {
            "connector": {"count": 1, "failures": 0},
            "model": {"count": 2, "failures": 1},
        }
    }
