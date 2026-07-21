"""Versioned runtime-log events and compatibility parsing."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Literal

RuntimeParseState = Literal[
    "structured_v1",
    "legacy_json",
    "legacy_plaintext",
    "malformed_json",
]
RuntimeTimestampState = Literal["observed", "unavailable", "invalid"]


@dataclass(frozen=True, slots=True)
class RuntimeEventV1:
    """One secret-safe structured runtime event persisted as JSONL."""

    observed_at: str
    component: str
    stream: str
    level: str
    event_kind: str
    message: str
    action: str | None = None
    duration_ms: float | None = None
    correlation_id: str | None = None
    operation_id: str | None = None
    receipt_id: str | None = None
    trace_id: str | None = None
    workspace_hash: str | None = None
    repository_hash: str | None = None


@dataclass(frozen=True, slots=True)
class ParsedRuntimeEvent:
    """One runtime line projected without inventing unavailable evidence."""

    timestamp: str | None
    timestamp_state: RuntimeTimestampState
    parse_state: RuntimeParseState
    component: str | None
    stream: str | None
    level: str
    event_kind: str | None
    action: str | None
    message: str
    duration_ms: float | None
    correlation_id: str | None
    operation_id: str | None
    receipt_id: str | None
    trace_id: str | None
    workspace_hash: str | None
    repository_hash: str | None


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _timestamp(value: object) -> tuple[str | None, RuntimeTimestampState]:
    if not isinstance(value, str) or not value:
        return None, "unavailable"
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None, "invalid"
    if parsed.tzinfo is None:
        return None, "invalid"
    return value, "observed"


def _duration(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float) or value < 0:
        return None
    return float(value)


def encode_runtime_event(event: RuntimeEventV1) -> str:
    """Encode one v1 event as deterministic compact JSON without a trailing newline."""

    payload = {"schema_version": 1, **asdict(event)}
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def parse_runtime_event(line: str) -> ParsedRuntimeEvent:
    """Parse v1, legacy JSON, malformed JSON and plaintext without false certainty."""

    try:
        raw = json.loads(line)
    except json.JSONDecodeError:
        parse_state: RuntimeParseState = (
            "malformed_json" if line.lstrip().startswith(("{", "[")) else "legacy_plaintext"
        )
        return ParsedRuntimeEvent(
            timestamp=None,
            timestamp_state="unavailable",
            parse_state=parse_state,
            component=None,
            stream=None,
            level="INFO",
            event_kind=None,
            action=None,
            message=line,
            duration_ms=None,
            correlation_id=None,
            operation_id=None,
            receipt_id=None,
            trace_id=None,
            workspace_hash=None,
            repository_hash=None,
        )

    if not isinstance(raw, dict):
        return ParsedRuntimeEvent(
            timestamp=None,
            timestamp_state="unavailable",
            parse_state="malformed_json",
            component=None,
            stream=None,
            level="INFO",
            event_kind=None,
            action=None,
            message=line,
            duration_ms=None,
            correlation_id=None,
            operation_id=None,
            receipt_id=None,
            trace_id=None,
            workspace_hash=None,
            repository_hash=None,
        )

    structured = raw.get("schema_version") == 1
    timestamp, timestamp_state = _timestamp(
        raw.get("observed_at") if structured else raw.get("timestamp")
    )
    message_value = raw.get("message")
    if not isinstance(message_value, str):
        message_value = raw.get("msg")
    message = message_value if isinstance(message_value, str) else ""
    level_value = raw.get("level", "INFO")
    level = str(level_value)[:30] or "INFO"

    return ParsedRuntimeEvent(
        timestamp=timestamp,
        timestamp_state=timestamp_state,
        parse_state="structured_v1" if structured else "legacy_json",
        component=_optional_string(raw.get("component")) if structured else None,
        stream=_optional_string(raw.get("stream")) if structured else None,
        level=level,
        event_kind=_optional_string(raw.get("event_kind")) if structured else None,
        action=_optional_string(raw.get("action")),
        message=message,
        duration_ms=_duration(raw.get("duration_ms")),
        correlation_id=_optional_string(raw.get("correlation_id")) if structured else None,
        operation_id=_optional_string(raw.get("operation_id")) if structured else None,
        receipt_id=_optional_string(raw.get("receipt_id")) if structured else None,
        trace_id=_optional_string(raw.get("trace_id")) if structured else None,
        workspace_hash=_optional_string(raw.get("workspace_hash")) if structured else None,
        repository_hash=_optional_string(raw.get("repository_hash")) if structured else None,
    )
