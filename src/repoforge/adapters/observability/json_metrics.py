"""Crash-safe bounded aggregate operation metrics.

Persists two views of the same recorded calls in one private, atomic,
lock-guarded JSON file:

- ``operations``: lifetime totals per action, unbounded in time (unchanged
  since schema version 1, kept for backward compatibility).
- ``buckets``: per-day totals per action, bounded to a fixed retention
  window (pruned on every write) so a before/after comparison across a
  shipped fix is possible without unbounded growth.

A version-1 file (``operations`` only) loads without error; lifetime totals
keep accumulating and ``buckets`` starts empty, then fills in as new calls
are recorded.

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

from ...ports.clock import Clock
from ...ports.locking import LockManager
from ..system import SystemClock

_SCHEMA_VERSION = 2
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
        "failure_categories": {},
    }


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
        return {"version": _SCHEMA_VERSION, "operations": {}, "buckets": {}}

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
        return {
            "version": _SCHEMA_VERSION,
            "operations": raw["operations"],
            "buckets": buckets if isinstance(buckets, dict) else {},
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
