"""Crash-safe bounded aggregate operation metrics."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from ...ports.locking import LockManager


class JsonMetricsSink:
    def __init__(self, state_root: Path, locks: LockManager):
        self.path = state_root / "operation-metrics.json"
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.path.parent, 0o700)
        self._locks = locks

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
        return {"version": 1, "operations": {}}

    def snapshot(self) -> dict[str, Any]:
        if not self.path.is_file():
            return self._empty()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return self._empty()
        if not isinstance(raw, dict) or not isinstance(raw.get("operations"), dict):
            return self._empty()
        return raw

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

    def record(
        self,
        action: str,
        *,
        success: bool,
        duration_ms: float,
        error_code: str | None,
    ) -> None:
        with self._locks.lock("operation-metrics", timeout_seconds=2):
            payload = self.snapshot()
            operations = payload.setdefault("operations", {})
            current = operations.setdefault(
                action,
                {
                    "count": 0,
                    "successes": 0,
                    "failures": 0,
                    "duration_ms_total": 0.0,
                    "duration_ms_max": 0.0,
                    "failure_categories": {},
                },
            )
            current["count"] = int(current["count"]) + 1
            key = "successes" if success else "failures"
            current[key] = int(current[key]) + 1
            rounded = round(max(0.0, float(duration_ms)), 3)
            current["duration_ms_total"] = round(float(current["duration_ms_total"]) + rounded, 3)
            current["duration_ms_max"] = max(float(current["duration_ms_max"]), rounded)
            if not success:
                category = error_code or "INTERNAL_ERROR"
                categories = current["failure_categories"]
                categories[category] = int(categories.get(category, 0)) + 1
            self._write(payload)
