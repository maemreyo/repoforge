"""Sanitized deterministic workflow-recording schema and safety invariants."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from enum import Enum
from typing import Any

from .errors import ErrorCode, RepoForgeError

WORKFLOW_RECORDING_SCHEMA_VERSION = 1
MAX_WORKFLOW_EVENTS = 256
MAX_WORKFLOW_RECORD_BYTES = 256 * 1024
MAX_WORKFLOW_INVENTORY_IDS = 128
MAX_WORKFLOW_ARGUMENTS = 64
MAX_WORKFLOW_NEXT_ACTIONS = 32
MAX_WORKFLOW_CAPABILITIES = 64
MAX_WORKFLOW_OFFSET_MS = 7 * 24 * 60 * 60 * 1000

_RECORDING_ID = re.compile(r"^wr-[a-f0-9]{24}$")
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SAFE_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_ERROR_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")
_SAFE_REFERENCE = re.compile(r"^(?:[A-Za-z0-9][A-Za-z0-9._:-]{0,127}|sha256:[a-f0-9]{64})$")

_SECRET_NAMES = {
    "authorization",
    "control_plane_api_key",
    "api_key",
    "apikey",
    "access_token",
    "token",
    "secret",
    "password",
    "credential",
    "credentials",
}
_CONTENT_NAMES = {
    "prompt",
    "system_prompt",
    "chain_of_thought",
    "reasoning",
    "source",
    "source_code",
    "body",
    "content",
    "patch",
    "diff",
    "stdout",
    "stderr",
    "log",
    "logs",
    "environment",
    "network_payload",
    "provider_payload",
}
_PATH_NAMES = {
    "path",
    "paths",
    "filepath",
    "file_path",
    "directory",
    "cwd",
    "working_directory",
}


class WorkflowArgumentCategory(str, Enum):
    NULL = "null"
    BOOLEAN = "boolean"
    INTEGER = "integer"
    NUMBER = "number"
    SAFE_ID = "safe_id"
    SHA256 = "sha256"
    COLLECTION_SHAPE = "collection_shape"
    OMITTED_SECRET = "omitted_secret"
    OMITTED_CONTENT = "omitted_content"
    OMITTED_PATH = "omitted_path"
    UNKNOWN_OMITTED = "unknown_omitted"


class WorkflowResultCategory(str, Enum):
    SUCCESS = "success"
    VALIDATION_ERROR = "validation_error"
    POLICY_ERROR = "policy_error"
    PROVIDER_ERROR = "provider_error"
    NOT_FOUND = "not_found"
    STALE = "stale"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"
    INTERNAL_ERROR = "internal_error"


class WorkflowStateTransition(str, Enum):
    NONE = "none"
    STARTED = "started"
    PROGRESSED = "progressed"
    RETRIED = "retried"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class WorkflowFinalOutcome(str, Enum):
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INCOMPLETE = "incomplete"


@dataclass(frozen=True, slots=True)
class WorkflowArgumentSummary:
    name: str
    category: WorkflowArgumentCategory
    value_hash: str
    truncated: bool = False


@dataclass(frozen=True, slots=True)
class WorkflowEvent:
    timestamp_offset_ms: int
    tool_inventory_ids: tuple[str, ...]
    selected_tool_id: str
    arguments: tuple[WorkflowArgumentSummary, ...]
    result_category: WorkflowResultCategory
    stable_error_code: str | None
    workspace_ref: str | None
    task_ref: str | None
    snapshot_ref: str | None
    next_action_ids: tuple[str, ...]
    state_transition: WorkflowStateTransition
    arguments_truncated: bool
    result_truncated: bool


@dataclass(frozen=True, slots=True)
class WorkflowMetrics:
    tool_calls: int
    duration_ms: int
    retry_count: int
    error_count: int


@dataclass(frozen=True, slots=True)
class WorkflowRecording:
    recording_id: str
    scenario_id: str
    server_instructions_hash: str
    tool_surface_hash: str
    capability_flags: tuple[str, ...]
    events: tuple[WorkflowEvent, ...]
    final_outcome: WorkflowFinalOutcome
    metrics: WorkflowMetrics
    created_at: str
    truncated: bool
    truncation_reason: str | None
    schema_version: int = WORKFLOW_RECORDING_SCHEMA_VERSION


def _invalid(
    message: str, *, code: ErrorCode = ErrorCode.WORKFLOW_RECORD_INVALID
) -> RepoForgeError:
    return RepoForgeError(
        message,
        code=code,
        safe_next_action="Discard the unsafe record and rebuild it from typed sanitized categories.",
    )


def _hash(category: WorkflowArgumentCategory, canonical: object) -> str:
    encoded = json.dumps(
        {"category": category.value, "value": canonical},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _shape(value: object, *, depth: int = 0) -> tuple[object, bool]:
    if depth >= 4:
        return {"kind": "depth_limited"}, True
    if isinstance(value, dict):
        values = list(value.values())
        limited = values[:32]
        children = [_shape(item, depth=depth + 1)[0] for item in limited]
        return {
            "kind": "mapping",
            "count": len(values),
            "children": children,
        }, len(values) > len(limited)
    if isinstance(value, (list, tuple, set, frozenset)):
        values = list(value)
        limited = values[:32]
        children = [_shape(item, depth=depth + 1)[0] for item in limited]
        return {
            "kind": "sequence",
            "count": len(values),
            "children": children,
        }, len(values) > len(limited)
    if value is None:
        return "null", False
    if isinstance(value, bool):
        return "boolean", False
    if isinstance(value, int):
        return "integer", False
    if isinstance(value, float):
        return "number", False
    if isinstance(value, str):
        return "string", False
    return "unknown", True


def normalize_workflow_argument(name: str, value: object) -> WorkflowArgumentSummary:
    """Summarize one validated argument without persisting its raw value."""
    normalized_name = name.strip().lower().replace("-", "_")
    if _SAFE_NAME.fullmatch(normalized_name) is None:
        raise _invalid("workflow argument name has an invalid format")
    if normalized_name in _SECRET_NAMES:
        category = WorkflowArgumentCategory.OMITTED_SECRET
        return WorkflowArgumentSummary(normalized_name, category, _hash(category, "omitted"))
    if normalized_name in _CONTENT_NAMES:
        category = WorkflowArgumentCategory.OMITTED_CONTENT
        return WorkflowArgumentSummary(normalized_name, category, _hash(category, "omitted"))
    if normalized_name in _PATH_NAMES or (
        isinstance(value, str)
        and (value.startswith(("/", "~/", "\\\\")) or re.match(r"^[A-Za-z]:[\\/]", value))
    ):
        category = WorkflowArgumentCategory.OMITTED_PATH
        return WorkflowArgumentSummary(normalized_name, category, _hash(category, "omitted"))
    if value is None:
        category = WorkflowArgumentCategory.NULL
        return WorkflowArgumentSummary(normalized_name, category, _hash(category, None))
    if isinstance(value, bool):
        category = WorkflowArgumentCategory.BOOLEAN
        return WorkflowArgumentSummary(normalized_name, category, _hash(category, value))
    if isinstance(value, int):
        category = WorkflowArgumentCategory.INTEGER
        return WorkflowArgumentSummary(normalized_name, category, _hash(category, value))
    if isinstance(value, float) and math.isfinite(value):
        category = WorkflowArgumentCategory.NUMBER
        return WorkflowArgumentSummary(normalized_name, category, _hash(category, value))
    if isinstance(value, str) and _SHA256.fullmatch(value.lower()):
        category = WorkflowArgumentCategory.SHA256
        return WorkflowArgumentSummary(normalized_name, category, _hash(category, value.lower()))
    if isinstance(value, str) and _SAFE_ID.fullmatch(value):
        category = WorkflowArgumentCategory.SAFE_ID
        return WorkflowArgumentSummary(normalized_name, category, _hash(category, value))
    if isinstance(value, (dict, list, tuple, set, frozenset)):
        shape, truncated = _shape(value)
        category = WorkflowArgumentCategory.COLLECTION_SHAPE
        return WorkflowArgumentSummary(
            normalized_name,
            category,
            _hash(category, shape),
            truncated=truncated,
        )
    category = WorkflowArgumentCategory.UNKNOWN_OMITTED
    return WorkflowArgumentSummary(normalized_name, category, _hash(category, "omitted"), True)


def _safe_id(value: str, field: str) -> str:
    if not isinstance(value, str) or _SAFE_ID.fullmatch(value) is None:
        raise _invalid(f"{field} has an invalid format")
    return value


def _sorted_ids(values: tuple[str, ...], field: str, *, maximum: int) -> tuple[str, ...]:
    if not isinstance(values, tuple):
        raise _invalid(f"{field} must be a tuple")
    normalized = tuple(sorted(set(_safe_id(item, field) for item in values)))
    if len(normalized) > maximum:
        raise _invalid(f"{field} exceeds the maximum of {maximum}")
    return normalized


def _safe_reference(value: str | None, field: str) -> str | None:
    if value is None:
        return None
    if _SAFE_REFERENCE.fullmatch(value) is None:
        raise _invalid(f"{field} is not a safe identifier or hash reference")
    return value


def validate_workflow_argument(summary: WorkflowArgumentSummary) -> WorkflowArgumentSummary:
    name = summary.name.strip().lower().replace("-", "_")
    if _SAFE_NAME.fullmatch(name) is None:
        raise _invalid("workflow argument name has an invalid format")
    if not isinstance(summary.category, WorkflowArgumentCategory):
        raise _invalid("workflow argument category is invalid")
    if _SHA256.fullmatch(summary.value_hash) is None:
        raise _invalid("workflow argument value_hash must be a full lowercase SHA-256")
    if not isinstance(summary.truncated, bool):
        raise _invalid("workflow argument truncated must be a boolean")
    return replace(summary, name=name)


def validate_workflow_event(event: WorkflowEvent) -> WorkflowEvent:
    if (
        not isinstance(event.timestamp_offset_ms, int)
        or isinstance(event.timestamp_offset_ms, bool)
        or not 0 <= event.timestamp_offset_ms <= MAX_WORKFLOW_OFFSET_MS
    ):
        raise _invalid("workflow event timestamp_offset_ms is outside the reviewed bound")
    inventory = _sorted_ids(
        event.tool_inventory_ids,
        "tool_inventory_ids",
        maximum=MAX_WORKFLOW_INVENTORY_IDS,
    )
    selected = _safe_id(event.selected_tool_id, "selected_tool_id")
    if selected not in inventory:
        raise _invalid("selected_tool_id must be present in tool_inventory_ids")
    if not isinstance(event.arguments, tuple):
        raise _invalid("workflow event arguments must be a tuple")
    arguments = tuple(
        sorted(
            (validate_workflow_argument(item) for item in event.arguments),
            key=lambda item: item.name,
        )
    )
    if len(arguments) > MAX_WORKFLOW_ARGUMENTS:
        raise _invalid(f"workflow event arguments exceed the maximum of {MAX_WORKFLOW_ARGUMENTS}")
    if len({item.name for item in arguments}) != len(arguments):
        raise _invalid("workflow event argument names must be unique")
    if not isinstance(event.result_category, WorkflowResultCategory):
        raise _invalid("workflow event result_category is invalid")
    if event.result_category is WorkflowResultCategory.SUCCESS:
        if event.stable_error_code is not None:
            raise _invalid("successful workflow events cannot contain an error code")
    elif (
        not isinstance(event.stable_error_code, str)
        or _ERROR_CODE.fullmatch(event.stable_error_code) is None
    ):
        raise _invalid("non-success workflow events require a stable error code")
    next_actions = _sorted_ids(
        event.next_action_ids,
        "next_action_ids",
        maximum=MAX_WORKFLOW_NEXT_ACTIONS,
    )
    if not isinstance(event.state_transition, WorkflowStateTransition):
        raise _invalid("workflow event state_transition is invalid")
    if not isinstance(event.arguments_truncated, bool) or not isinstance(
        event.result_truncated, bool
    ):
        raise _invalid("workflow event truncation flags must be booleans")
    return replace(
        event,
        tool_inventory_ids=inventory,
        arguments=arguments,
        workspace_ref=_safe_reference(event.workspace_ref, "workspace_ref"),
        task_ref=_safe_reference(event.task_ref, "task_ref"),
        snapshot_ref=_safe_reference(event.snapshot_ref, "snapshot_ref"),
        next_action_ids=next_actions,
    )


def _validate_metrics(
    metrics: WorkflowMetrics, events: tuple[WorkflowEvent, ...], truncated: bool
) -> None:
    values = (metrics.tool_calls, metrics.duration_ms, metrics.retry_count, metrics.error_count)
    if any(not isinstance(value, int) or isinstance(value, bool) or value < 0 for value in values):
        raise _invalid("workflow metrics must be non-negative integers")
    if metrics.tool_calls < len(events):
        raise _invalid("workflow metrics tool_calls cannot be lower than recorded events")
    if not truncated and metrics.tool_calls != len(events):
        raise _invalid("complete workflow metrics tool_calls must equal recorded events")
    observed_errors = sum(
        event.result_category is not WorkflowResultCategory.SUCCESS for event in events
    )
    if metrics.error_count < observed_errors:
        raise _invalid("workflow metrics error_count cannot be lower than recorded errors")
    if not truncated and metrics.error_count != observed_errors:
        raise _invalid("complete workflow metrics error_count must equal recorded errors")
    if metrics.retry_count > metrics.tool_calls:
        raise _invalid("workflow metrics retry_count cannot exceed tool_calls")
    if events and metrics.duration_ms < events[-1].timestamp_offset_ms:
        raise _invalid("workflow metrics duration_ms cannot precede the final event")


def validate_workflow_recording(recording: WorkflowRecording) -> WorkflowRecording:
    if recording.schema_version != WORKFLOW_RECORDING_SCHEMA_VERSION or isinstance(
        recording.schema_version, bool
    ):
        raise _invalid(
            f"Unsupported workflow recording schema version: {recording.schema_version!r}",
            code=ErrorCode.WORKFLOW_RECORD_SCHEMA_UNSUPPORTED,
        )
    if _RECORDING_ID.fullmatch(recording.recording_id) is None:
        raise _invalid("recording_id has an invalid format")
    scenario_id = _safe_id(recording.scenario_id, "scenario_id")
    if _SHA256.fullmatch(recording.server_instructions_hash) is None:
        raise _invalid("server_instructions_hash must be a full lowercase SHA-256")
    if _SHA256.fullmatch(recording.tool_surface_hash) is None:
        raise _invalid("tool_surface_hash must be a full lowercase SHA-256")
    capabilities = _sorted_ids(
        recording.capability_flags,
        "capability_flags",
        maximum=MAX_WORKFLOW_CAPABILITIES,
    )
    if not isinstance(recording.events, tuple) or not recording.events:
        raise _invalid("workflow recording must contain at least one event")
    if len(recording.events) > MAX_WORKFLOW_EVENTS:
        raise _invalid(
            f"workflow recording exceeds the maximum of {MAX_WORKFLOW_EVENTS} events",
            code=ErrorCode.WORKFLOW_RECORD_TOO_LARGE,
        )
    events = tuple(validate_workflow_event(event) for event in recording.events)
    offsets = tuple(event.timestamp_offset_ms for event in events)
    if tuple(sorted(offsets)) != offsets:
        raise _invalid("workflow event timestamp offsets must be monotonic")
    if not isinstance(recording.final_outcome, WorkflowFinalOutcome):
        raise _invalid("workflow final_outcome is invalid")
    if not isinstance(recording.truncated, bool):
        raise _invalid("workflow recording truncated must be a boolean")
    truncation_reason = recording.truncation_reason
    if recording.truncated:
        if recording.final_outcome is not WorkflowFinalOutcome.INCOMPLETE:
            raise _invalid("truncated workflow recordings must have incomplete final_outcome")
        if truncation_reason is None or _SAFE_ID.fullmatch(truncation_reason) is None:
            raise _invalid("truncated workflow recordings require a safe truncation_reason")
    elif truncation_reason is not None:
        raise _invalid("complete workflow recordings cannot contain truncation_reason")
    try:
        created_at = datetime.fromisoformat(recording.created_at)
    except (TypeError, ValueError) as exc:
        raise _invalid("workflow recording created_at must be an ISO-8601 timestamp") from exc
    if created_at.tzinfo is None:
        raise _invalid("workflow recording created_at must include a timezone offset")
    _validate_metrics(recording.metrics, events, recording.truncated)
    return replace(
        recording,
        scenario_id=scenario_id,
        capability_flags=capabilities,
        events=events,
        truncation_reason=truncation_reason,
    )


def new_workflow_recording(
    *,
    recording_id: str,
    scenario_id: str,
    server_instructions_hash: str,
    tool_surface_hash: str,
    capability_flags: tuple[str, ...],
    events: tuple[WorkflowEvent, ...],
    final_outcome: WorkflowFinalOutcome,
    metrics: WorkflowMetrics,
    created_at: str,
    truncated: bool = False,
    truncation_reason: str | None = None,
) -> WorkflowRecording:
    return validate_workflow_recording(
        WorkflowRecording(
            recording_id=recording_id,
            scenario_id=scenario_id,
            server_instructions_hash=server_instructions_hash,
            tool_surface_hash=tool_surface_hash,
            capability_flags=capability_flags,
            events=events,
            final_outcome=final_outcome,
            metrics=metrics,
            created_at=created_at,
            truncated=truncated,
            truncation_reason=truncation_reason,
        )
    )


def workflow_recording_payload(recording: WorkflowRecording) -> dict[str, Any]:
    normalized = validate_workflow_recording(recording)
    payload = asdict(normalized)
    payload["final_outcome"] = normalized.final_outcome.value
    payload["events"] = []
    for event in normalized.events:
        event_payload = asdict(event)
        event_payload["result_category"] = event.result_category.value
        event_payload["state_transition"] = event.state_transition.value
        event_payload["arguments"] = [
            {
                "name": argument.name,
                "category": argument.category.value,
                "value_hash": argument.value_hash,
                "truncated": argument.truncated,
            }
            for argument in event.arguments
        ]
        event_payload["tool_inventory_ids"] = list(event.tool_inventory_ids)
        event_payload["next_action_ids"] = list(event.next_action_ids)
        payload["events"].append(event_payload)
    payload["capability_flags"] = list(normalized.capability_flags)
    return payload


def canonical_workflow_recording_bytes(recording: WorkflowRecording) -> bytes:
    return json.dumps(
        workflow_recording_payload(recording),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
