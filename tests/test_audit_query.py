from __future__ import annotations

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
        '{"action": "ok", "success": true, "details": {}}\n'
        "not-json\n"
        "[1, 2, 3]\n",
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
    metrics.record(
        "workspace_commit", success=False, duration_ms=8.0, error_code="STALE_STATE"
    )

    rows = summarize_operation_metrics(metrics.snapshot())
    assert len(rows) == 1
    assert rows[0]["action"] == "workspace_commit"
    assert rows[0]["count"] == 2
    assert rows[0]["failures"] == 1
    assert rows[0]["top_error_codes"] == [["STALE_STATE", 1]]
