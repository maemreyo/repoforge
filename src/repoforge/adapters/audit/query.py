"""Bounded, redacted read access to local operational history for debugging.

This module only reads state that :mod:`repoforge.adapters.audit.jsonl` and
:mod:`repoforge.adapters.observability.json_metrics` already write; it adds no new
persistence and never touches Git, GitHub, or a workspace.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ...domain.errors import ConfigError

_MAX_SCAN_BYTES = 20_000_000


def read_audit_events(
    path: Path,
    *,
    limit: int = 20,
    action: str | None = None,
    only_failed: bool = False,
    min_duration_ms: float | None = None,
) -> list[dict[str, Any]]:
    """Return up to `limit` matching audit events, most recent first."""
    if limit <= 0 or limit > 1000:
        raise ConfigError("Audit query limit must be between 1 and 1000")
    if not path.is_file():
        return []
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - _MAX_SCAN_BYTES))
            text = handle.read().decode("utf-8", errors="replace")
    except OSError as exc:
        raise ConfigError(f"Cannot read audit log {path}: {exc}") from exc
    matched: list[dict[str, Any]] = []
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        if action is not None and event.get("action") != action:
            continue
        if only_failed and event.get("success", True):
            continue
        if min_duration_ms is not None:
            details = event.get("details")
            duration = details.get("duration_ms") if isinstance(details, dict) else None
            if not isinstance(duration, (int, float)) or duration < min_duration_ms:
                continue
        matched.append(event)
        if len(matched) >= limit:
            break
    return matched


def summarize_operation_metrics(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten an operation-metrics snapshot into rows sorted by average duration, slowest first."""
    operations = snapshot.get("operations")
    rows: list[dict[str, Any]] = []
    if not isinstance(operations, dict):
        return rows
    for action, stats in operations.items():
        if not isinstance(stats, dict):
            continue
        count = int(stats.get("count", 0) or 0)
        failures = int(stats.get("failures", 0) or 0)
        duration_total = float(stats.get("duration_ms_total", 0.0) or 0.0)
        categories = stats.get("failure_categories")
        top_error_codes = (
            sorted(categories.items(), key=lambda item: item[1], reverse=True)[:3]
            if isinstance(categories, dict)
            else []
        )
        rows.append(
            {
                "action": action,
                "count": count,
                "failures": failures,
                "failure_rate": round(failures / count, 4) if count else 0.0,
                "duration_ms_avg": round(duration_total / count, 3) if count else 0.0,
                "duration_ms_max": float(stats.get("duration_ms_max", 0.0) or 0.0),
                "top_error_codes": [list(item) for item in top_error_codes],
            }
        )
    rows.sort(key=lambda row: row["duration_ms_avg"], reverse=True)
    return rows
