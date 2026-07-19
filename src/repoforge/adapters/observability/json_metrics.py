"""Crash-safe bounded aggregate operation metrics.

Persists three views of recorded calls in one private, atomic, lock-guarded
JSON file:

- ``operations``: lifetime totals per action, unbounded in time (unchanged
  since schema version 1, kept for backward compatibility).
- ``buckets``: per-day totals per action, bounded to a fixed retention
  window (pruned on every write) so a before/after comparison across a
  shipped fix is possible without unbounded growth.
- ``latency``: bounded histograms and approximate p95 values for engine,
  connector, client round-trip, and emitted payload bytes. Raw trace IDs are
  never persisted.

Files from schema versions 1-3 load without error; lifetime totals keep
accumulating while missing or malformed newer views restart empty.

Each per-action stat also carries ``result_bytes_total``/``result_bytes_max``,
the compact-JSON size of successful results, alongside the existing duration
aggregates. A file recorded before these fields existed loads compatibly:
missing values default to ``0`` and continue accumulating from there.
"""

from __future__ import annotations

import json
import os
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from ...domain.latency import (
    DURATION_BUCKETS_MS,
    PAYLOAD_BUCKETS_BYTES,
    LatencyStatus,
    LatencyTrace,
    ToolPayloadClass,
    histogram_bucket,
    histogram_percentile,
    histogram_template,
    payload_budget,
)
from ...ports.clock import Clock
from ...ports.locking import LockManager
from ..system import SystemClock

_SCHEMA_VERSION = 4
DEFAULT_RETENTION_DAYS = 30


def _empty_stat() -> dict[str, Any]:
    return {
        "count": 0,
        "successes": 0,
        "failures": 0,
        "duration_ms_total": 0.0,
        "duration_ms_max": 0.0,
        "result_bytes_total": 0,
        "result_bytes_max": 0,
        "result_bytes_count": 0,
        "failure_categories": {},
    }


def _empty_latency_layer() -> dict[str, Any]:
    return {
        "observed_count": 0,
        "unobserved_count": 0,
        "unavailable_count": 0,
        "failed_count": 0,
        "histogram": histogram_template(DURATION_BUCKETS_MS),
        "p95_ms": 0.0,
    }


def _empty_tool_class_latency(tool_class: ToolPayloadClass) -> dict[str, Any]:
    return {
        "count": 0,
        "engine": _empty_latency_layer(),
        "connector": _empty_latency_layer(),
        "client_round_trip": _empty_latency_layer(),
        "payload": {
            "histogram": histogram_template(PAYLOAD_BUCKETS_BYTES),
            "p95_bytes": 0.0,
            "budget_bytes": payload_budget(tool_class),
            "over_budget_count": 0,
            "legacy_duplication_count": 0,
        },
    }


def _empty_latency() -> dict[str, Any]:
    return {"tool_classes": {}}


def _legacy_result_bytes_count(stats: dict[str, Any]) -> int:
    raw = stats.get("result_bytes_count")
    if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0:
        return raw
    total = stats.get("result_bytes_total", 0)
    maximum = stats.get("result_bytes_max", 0)
    if (
        isinstance(total, (int, float))
        and isinstance(maximum, (int, float))
        and (total > 0 or maximum > 0)
    ):
        return max(0, int(stats.get("successes", 0) or 0))
    return 0


class JsonMetricsSink:
    def __init__(
        self,
        state_root: Path,
        locks: LockManager,
        clock: Clock | None = None,
        *,
        retention_days: int = DEFAULT_RETENTION_DAYS,
    ):
        self.path = state_root / "operation-metrics.json"
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.path.parent, 0o700)
        self._locks = locks
        self._clock = clock or SystemClock()
        self._retention_days = max(1, int(retention_days))

    @staticmethod
    def _fsync_dir(path: Path) -> None:
        try:
            descriptor = os.open(path, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(descriptor)
        except OSError:
            pass
        finally:
            os.close(descriptor)

    @staticmethod
    def _empty() -> dict[str, Any]:
        return {
            "version": _SCHEMA_VERSION,
            "operations": {},
            "buckets": {},
            "latency": _empty_latency(),
        }

    def snapshot(self) -> dict[str, Any]:
        if not self.path.is_file():
            return self._empty()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return self._empty()
        if not isinstance(raw, dict) or not isinstance(raw.get("operations"), dict):
            return self._empty()
        buckets = raw.get("buckets")
        latency = raw.get("latency")
        if not isinstance(latency, dict) or not isinstance(latency.get("tool_classes"), dict):
            latency = _empty_latency()
        return {
            "version": _SCHEMA_VERSION,
            "operations": raw["operations"],
            "buckets": buckets if isinstance(buckets, dict) else {},
            "latency": latency,
        }

    def _write(self, payload: dict[str, Any]) -> None:
        temporary = self.path.with_name(
            f".{self.path.name}.tmp-{os.getpid()}-{os.urandom(4).hex()}"
        )
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            os.chmod(self.path, 0o600)
            self._fsync_dir(self.path.parent)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _apply(
        container: dict[str, Any],
        action: str,
        *,
        success: bool,
        rounded_duration_ms: float,
        error_code: str | None,
        result_bytes: int | None = None,
    ) -> None:
        current = container.setdefault(action, _empty_stat())
        current["result_bytes_count"] = _legacy_result_bytes_count(current)
        current["count"] = int(current["count"]) + 1
        key = "successes" if success else "failures"
        current[key] = int(current[key]) + 1
        current["duration_ms_total"] = round(
            float(current["duration_ms_total"]) + rounded_duration_ms, 3
        )
        current["duration_ms_max"] = max(float(current["duration_ms_max"]), rounded_duration_ms)
        if result_bytes is not None:
            # `.get(..., 0)` tolerates a legacy stat entry recorded before these
            # fields existed, so a v1/v2 file without them still accumulates correctly.
            current["result_bytes_total"] = int(current.get("result_bytes_total", 0)) + int(
                result_bytes
            )
            current["result_bytes_max"] = max(
                int(current.get("result_bytes_max", 0)), int(result_bytes)
            )
            current["result_bytes_count"] = int(current["result_bytes_count"]) + 1
        if not success:
            category = error_code or "INTERNAL_ERROR"
            categories = current["failure_categories"]
            categories[category] = int(categories.get(category, 0)) + 1

    def _prune_buckets(self, buckets: dict[str, Any], today: date) -> None:
        cutoff = today - timedelta(days=self._retention_days - 1)
        stale = []
        for day in buckets:
            try:
                day_date = date.fromisoformat(day)
            except (TypeError, ValueError):
                stale.append(day)
                continue
            if day_date < cutoff:
                stale.append(day)
        for day in stale:
            del buckets[day]

    def record(
        self,
        action: str,
        *,
        success: bool,
        duration_ms: float,
        error_code: str | None,
        result_bytes: int | None = None,
    ) -> None:
        normalized_bytes = (
            int(result_bytes)
            if isinstance(result_bytes, (int, float)) and result_bytes >= 0
            else None
        )
        with self._locks.lock("operation-metrics", timeout_seconds=2):
            payload = self.snapshot()
            rounded = round(max(0.0, float(duration_ms)), 3)
            self._apply(
                payload.setdefault("operations", {}),
                action,
                success=success,
                rounded_duration_ms=rounded,
                error_code=error_code,
                result_bytes=normalized_bytes,
            )
            today_text = self._clock.now_iso()[:10]
            try:
                today = date.fromisoformat(today_text)
            except ValueError:
                today = None
            buckets = payload.setdefault("buckets", {})
            if today is not None:
                self._apply(
                    buckets.setdefault(today_text, {}),
                    action,
                    success=success,
                    rounded_duration_ms=rounded,
                    error_code=error_code,
                    result_bytes=normalized_bytes,
                )
                self._prune_buckets(buckets, today)
            payload["version"] = _SCHEMA_VERSION
            self._write(payload)

    def record_latency(self, trace: LatencyTrace) -> None:
        """Persist bounded aggregates only; never retain raw trace identities."""

        with self._locks.lock("operation-metrics", timeout_seconds=2):
            payload = self.snapshot()
            latency = payload.setdefault("latency", _empty_latency())
            tool_classes = latency.setdefault("tool_classes", {})
            stats = tool_classes.setdefault(
                trace.tool_class.value,
                _empty_tool_class_latency(trace.tool_class),
            )
            stats["count"] = int(stats.get("count", 0)) + 1

            for layer_name, observation in (
                ("engine", trace.engine),
                ("connector", trace.connector),
                ("client_round_trip", trace.client_round_trip),
            ):
                layer = stats.setdefault(layer_name, _empty_latency_layer())
                count_key = f"{observation.status.value}_count"
                layer[count_key] = int(layer.get(count_key, 0)) + 1
                if (
                    observation.status is LatencyStatus.OBSERVED
                    and observation.duration_ms is not None
                ):
                    histogram = layer.setdefault(
                        "histogram", histogram_template(DURATION_BUCKETS_MS)
                    )
                    bucket = histogram_bucket(observation.duration_ms, DURATION_BUCKETS_MS)
                    histogram[bucket] = int(histogram.get(bucket, 0)) + 1
                    layer["p95_ms"] = histogram_percentile(
                        histogram,
                        DURATION_BUCKETS_MS,
                        percentile=0.95,
                    )

            payload_stats = stats.setdefault(
                "payload",
                _empty_tool_class_latency(trace.tool_class)["payload"],
            )
            payload_histogram = payload_stats.setdefault(
                "histogram", histogram_template(PAYLOAD_BUCKETS_BYTES)
            )
            payload_bucket = histogram_bucket(
                float(trace.payload.emitted_bytes),
                PAYLOAD_BUCKETS_BYTES,
            )
            payload_histogram[payload_bucket] = int(payload_histogram.get(payload_bucket, 0)) + 1
            payload_stats["p95_bytes"] = histogram_percentile(
                payload_histogram,
                PAYLOAD_BUCKETS_BYTES,
                percentile=0.95,
            )
            payload_stats["budget_bytes"] = trace.payload.budget_bytes
            if not trace.payload.within_budget:
                payload_stats["over_budget_count"] = (
                    int(payload_stats.get("over_budget_count", 0)) + 1
                )
            if trace.payload.legacy_text_duplication:
                payload_stats["legacy_duplication_count"] = (
                    int(payload_stats.get("legacy_duplication_count", 0)) + 1
                )

            payload["version"] = _SCHEMA_VERSION
            self._write(payload)
