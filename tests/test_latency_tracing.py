from __future__ import annotations

import json

from repoforge.adapters.observability.json_metrics import JsonMetricsSink
from repoforge.domain.latency import (
    LatencyLayer,
    LatencyObservation,
    LatencyStatus,
    LatencyTrace,
    PayloadMetrics,
    ToolPayloadClass,
)
from repoforge.testing import FixedClock, InMemoryLockManager


def _trace(
    *,
    engine_ms: float,
    emitted_bytes: int,
    connector: LatencyObservation | None = None,
) -> LatencyTrace:
    return LatencyTrace(
        trace_id="trace-0123456789abcdef",
        tool_name="repo_list",
        tool_class=ToolPayloadClass.COMPACT,
        client_name="ChatGPT",
        client_version="fixture-1",
        engine=LatencyObservation.observed(LatencyLayer.ENGINE, engine_ms),
        connector=connector or LatencyObservation.unobserved(LatencyLayer.CONNECTOR),
        client_round_trip=LatencyObservation.unobserved(LatencyLayer.CLIENT_ROUND_TRIP),
        payload=PayloadMetrics(
            structured_bytes=max(0, emitted_bytes - 80),
            text_bytes=min(80, emitted_bytes),
            emitted_bytes=emitted_bytes,
            budget_bytes=16_384,
            within_budget=emitted_bytes <= 16_384,
            legacy_text_duplication=False,
        ),
    )


def test_latency_trace_keeps_unobserved_layers_explicit() -> None:
    trace = _trace(engine_ms=26.0, emitted_bytes=1080)

    payload = trace.as_dict()
    assert payload["engine"] == {
        "layer": "engine",
        "status": "observed",
        "duration_ms": 26.0,
        "error_code": None,
    }
    assert payload["connector"]["status"] == "unobserved"
    assert payload["client_round_trip"]["status"] == "unobserved"
    assert payload["payload"]["within_budget"] is True


def test_metrics_sink_aggregates_bounded_p95_and_connector_unavailable(tmp_path) -> None:
    sink = JsonMetricsSink(
        tmp_path,
        InMemoryLockManager(),
        FixedClock("2026-07-18T00:00:00+00:00"),
    )
    for duration in range(1, 21):
        sink.record_latency(_trace(engine_ms=float(duration), emitted_bytes=1000 + duration))
    sink.record_latency(
        _trace(
            engine_ms=30.0,
            emitted_bytes=20_000,
            connector=LatencyObservation(
                layer=LatencyLayer.CONNECTOR,
                status=LatencyStatus.UNAVAILABLE,
                duration_ms=None,
                error_code="UNAVAILABLE",
            ),
        )
    )

    compact = sink.snapshot()["latency"]["tool_classes"]["compact"]
    assert compact["count"] == 21
    assert compact["engine"]["observed_count"] == 21
    assert 19 <= compact["engine"]["p95_ms"] <= 50
    assert compact["connector"]["unavailable_count"] == 1
    assert compact["payload"]["p95_bytes"] >= 1020
    assert compact["payload"]["budget_bytes"] == 16_384
    assert compact["payload"]["over_budget_count"] == 1
    assert len(compact["engine"]["histogram"]) <= 16
    assert len(compact["payload"]["histogram"]) <= 16


def test_metrics_sink_recovers_malformed_legacy_latency_state(tmp_path) -> None:
    sink = JsonMetricsSink(
        tmp_path,
        InMemoryLockManager(),
        FixedClock("2026-07-18T00:00:00+00:00"),
    )
    sink.path.write_text(
        json.dumps(
            {
                "version": 3,
                "operations": {},
                "buckets": {},
                "latency": {"tool_classes": []},
            }
        ),
        encoding="utf-8",
    )

    sink.record_latency(_trace(engine_ms=12.0, emitted_bytes=1080))

    compact = sink.snapshot()["latency"]["tool_classes"]["compact"]
    assert compact["count"] == 1
    assert compact["engine"]["observed_count"] == 1
