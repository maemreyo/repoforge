"""Bounded, redacted read and prune access to local operational history for debugging.

This module only reads or prunes state that :mod:`repoforge.adapters.audit.jsonl` and
:mod:`repoforge.adapters.observability.json_metrics` already write; it adds no new
persistence and never touches Git, GitHub, or a workspace.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from ...domain.errors import ConfigError

_MAX_SCAN_BYTES = 20_000_000


def _result_bytes_count(stats: dict[str, Any]) -> int:
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


def read_audit_events(
    path: Path,
    *,
    limit: int = 20,
    action: str | None = None,
    only_failed: bool = False,
    min_duration_ms: float | None = None,
    min_bytes: float | None = None,
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
        if min_bytes is not None:
            details = event.get("details")
            result_bytes = details.get("result_bytes") if isinstance(details, dict) else None
            if not isinstance(result_bytes, (int, float)) or result_bytes < min_bytes:
                continue
        matched.append(event)
        if len(matched) >= limit:
            break
    return matched


def summarize_command_source_stats(path: Path) -> list[dict[str, Any]]:
    """Aggregate dirty vs. clean ``workspace_run_profile`` run counts per profile (issue #170).

    Reads only the ``command_source_dirty``/``profile`` fields already written to each
    ``workspace_run_profile`` audit event's ``details`` -- no new persistence, and no
    behavior change if those fields are absent (e.g. a legacy event, or a profile with no
    declared/derived command-source paths never stamps dirty, only clean).
    """
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
    counts: dict[str, dict[str, int]] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict) or event.get("action") != "workspace_run_profile":
            continue
        details = event.get("details")
        if not isinstance(details, dict):
            continue
        profile = details.get("profile")
        dirty = details.get("command_source_dirty")
        if not isinstance(profile, str) or not isinstance(dirty, bool):
            continue
        bucket = counts.setdefault(profile, {"dirty": 0, "clean": 0})
        bucket["dirty" if dirty else "clean"] += 1
    return [
        {"profile": profile, "dirty": data["dirty"], "clean": data["clean"]}
        for profile, data in sorted(counts.items())
    ]


def _rows_from_operations(operations: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for action, stats in operations.items():
        if not isinstance(stats, dict):
            continue
        count = int(stats.get("count", 0) or 0)
        failures = int(stats.get("failures", 0) or 0)
        duration_total = float(stats.get("duration_ms_total", 0.0) or 0.0)
        # `.get(..., 0)` tolerates a legacy stat entry recorded before result-size
        # tracking existed, so it reports 0 rather than raising.
        result_bytes_total = float(stats.get("result_bytes_total", 0) or 0)
        result_bytes_count = _result_bytes_count(stats)
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
                "result_bytes_avg": (
                    round(result_bytes_total / result_bytes_count, 3) if result_bytes_count else 0.0
                ),
                "result_bytes_max": int(stats.get("result_bytes_max", 0) or 0),
                "top_error_codes": [list(item) for item in top_error_codes],
            }
        )
    rows.sort(key=lambda row: row["duration_ms_avg"], reverse=True)
    return rows


def _empty_bucket_stat() -> dict[str, Any]:
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


def _merge_bucket_stat(target: dict[str, Any], source: dict[str, Any]) -> None:
    target["count"] += int(source.get("count", 0) or 0)
    target["successes"] += int(source.get("successes", 0) or 0)
    target["failures"] += int(source.get("failures", 0) or 0)
    target["duration_ms_total"] += float(source.get("duration_ms_total", 0.0) or 0.0)
    target["duration_ms_max"] = max(
        float(target["duration_ms_max"]), float(source.get("duration_ms_max", 0.0) or 0.0)
    )
    # `.get(..., 0)` tolerates a legacy day bucket recorded before result-size
    # tracking existed, so it merges as 0 rather than raising.
    target["result_bytes_total"] += float(source.get("result_bytes_total", 0) or 0)
    target["result_bytes_max"] = max(
        float(target["result_bytes_max"]), float(source.get("result_bytes_max", 0) or 0)
    )
    target["result_bytes_count"] += _result_bytes_count(source)
    categories = source.get("failure_categories")
    if isinstance(categories, dict):
        target_categories = target["failure_categories"]
        for code, occurrences in categories.items():
            target_categories[code] = int(target_categories.get(code, 0)) + int(occurrences or 0)


def _parse_window_date(value: str, *, field: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ConfigError(f"Invalid --{field} date {value!r}; expected YYYY-MM-DD") from exc


def summarize_operation_metrics(
    snapshot: dict[str, Any],
    *,
    since: str | None = None,
    until: str | None = None,
) -> list[dict[str, Any]]:
    """Flatten an operation-metrics snapshot into rows sorted by average duration, slowest first.

    Without `since`/`until` this aggregates the lifetime `operations` totals — the original,
    contract-stable behavior. Passing either bound instead aggregates only the daily `buckets`
    whose date falls within `[since, until]` (a bound left as `None` is open-ended), so an
    operator can isolate a specific window (for example, the days after a fix shipped) without
    the lifetime totals diluting the comparison.
    """
    if since is None and until is None:
        operations = snapshot.get("operations")
        if not isinstance(operations, dict):
            return []
        return _rows_from_operations(operations)

    since_date = _parse_window_date(since, field="since") if since is not None else None
    until_date = _parse_window_date(until, field="until") if until is not None else None
    if since_date is not None and until_date is not None and since_date > until_date:
        raise ConfigError(f"--since {since} must not be after --until {until}")

    buckets = snapshot.get("buckets")
    aggregated: dict[str, dict[str, Any]] = {}
    if isinstance(buckets, dict):
        for day, actions in buckets.items():
            try:
                day_date = date.fromisoformat(day)
            except (TypeError, ValueError):
                continue
            if since_date is not None and day_date < since_date:
                continue
            if until_date is not None and day_date > until_date:
                continue
            if not isinstance(actions, dict):
                continue
            for action, stats in actions.items():
                if not isinstance(stats, dict):
                    continue
                _merge_bucket_stat(aggregated.setdefault(action, _empty_bucket_stat()), stats)
    return _rows_from_operations(aggregated)


def prune_audit_log(
    path: Path,
    *,
    before: str,
) -> int:
    """Remove audit events older than the ISO-8601 ``before`` timestamp.

    Returns the number of pruned events.
    """
    try:
        cutoff = datetime.fromisoformat(before.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ConfigError(f"Invalid --before date {before!r}; expected ISO-8601") from exc
    if cutoff.tzinfo is None:
        raise ConfigError("--before must include a timezone offset (e.g. 2026-07-16T08:00:00+00:00)")
    if not path.is_file():
        return 0
    try:
        with path.open("r") as handle:
            lines = handle.readlines()
    except OSError as exc:
        raise ConfigError(f"Cannot read audit log {path}: {exc}") from exc

    kept: list[str] = []
    pruned = 0
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            pruned += 1
            continue
        ts = event.get("timestamp", "")
        try:
            event_time = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            pruned += 1
            continue
        if event_time < cutoff:
            pruned += 1
        else:
            kept.append(line + "\n")

    try:
        with path.open("w") as handle:
            handle.writelines(kept)
    except OSError as exc:
        raise ConfigError(f"Cannot write audit log {path}: {exc}") from exc
    return pruned
