"""Typed durable operation state and transition policy."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import Enum

from .errors import ErrorCode, RepoForgeError
from .redaction import redact_text

LEGACY_OPERATION_SCHEMA_VERSION = 1
OPERATION_SCHEMA_VERSION = 2
_OPERATION_ID = re.compile(r"^op-[a-f0-9]{24}$")
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SAFE_KIND = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_SAFE_PHASE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_SHA40 = re.compile(r"^[a-f0-9]{40}$")
_SHA64 = re.compile(r"^[a-f0-9]{64}$")
_PRIVATE_KEY = re.compile(
    r"-----BEGIN [^-\r\n]*PRIVATE KEY-----.*?-----END [^-\r\n]*PRIVATE KEY-----",
    re.DOTALL,
)
_TOKEN_CANDIDATE = re.compile(r"(?<![A-Za-z0-9])([A-Za-z0-9_./+=-]{32,})(?![A-Za-z0-9])")
_RECORD_PROVENANCE = frozenset({"current", "legacy_migrated", "recovered_inconsistent"})
_RECORD_CONSISTENCY = frozenset({"consistent", "record_inconsistent"})
_RECORD_DIAGNOSTIC = re.compile(r"^[a-z][a-z0-9_]{0,127}$")
_RECEIPT_ID = re.compile(r"^receipt-[a-f0-9]{24}$")


class OperationState(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    ORPHANED = "orphaned"


class OperationRetryability(str, Enum):
    NONE = "none"
    MANUAL = "manual"
    AUTOMATIC = "automatic"


TERMINAL_OPERATION_STATES = frozenset(
    {
        OperationState.SUCCEEDED,
        OperationState.FAILED,
        OperationState.CANCELLED,
        OperationState.EXPIRED,
        OperationState.ORPHANED,
    }
)

_ALLOWED_TRANSITIONS: dict[OperationState, frozenset[OperationState]] = {
    OperationState.PENDING: frozenset(
        {
            OperationState.RUNNING,
            OperationState.FAILED,
            OperationState.CANCELLED,
            OperationState.EXPIRED,
        }
    ),
    OperationState.RUNNING: frozenset(
        {
            OperationState.SUCCEEDED,
            OperationState.FAILED,
            OperationState.CANCELLED,
            OperationState.EXPIRED,
            OperationState.ORPHANED,
        }
    ),
}


@dataclass(frozen=True, slots=True)
class OperationSnapshotBinding:
    head_sha: str | None = None
    workspace_fingerprint: str | None = None
    config_generation: int | None = None
    evidence_snapshot_id: str | None = None


@dataclass(frozen=True, slots=True)
class OperationTask:
    operation_id: str
    kind: str
    state: OperationState
    phase: str
    progress_current: int
    progress_total: int | None
    progress_unit: str | None
    progress_message: str | None
    task_id: str | None
    workspace_id: str | None
    snapshot_binding: OperationSnapshotBinding | None
    result_reference: str | None
    error_code: str | None
    error_message: str | None
    retryability: OperationRetryability
    cancel_supported: bool
    cancellation_requested_at: str | None
    created_at: str
    updated_at: str
    expires_at: str | None
    receipt_id: str | None = None
    record_provenance: str = "current"
    record_consistency: str = "consistent"
    record_diagnostics: tuple[str, ...] = ()
    schema_version: int = OPERATION_SCHEMA_VERSION


@dataclass(frozen=True, slots=True)
class OperationCancellationDecision:
    task: OperationTask
    cancellation_requested: bool
    already_requested: bool
    already_terminal: bool
    cancel_supported: bool


def _error(message: str, *, code: ErrorCode = ErrorCode.OPERATION_INVALID) -> RepoForgeError:
    return RepoForgeError(message, code=code)


def _parse_timestamp(value: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise _error(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise _error(f"{field} must include a timezone offset")
    return parsed


def next_operation_timestamp(previous: str, candidate: str) -> str:
    """Return a strictly increasing timestamp for optimistic CAS identity."""
    previous_dt = _parse_timestamp(previous, "updated_at")
    candidate_dt = _parse_timestamp(candidate, "now")
    if candidate_dt <= previous_dt:
        candidate_dt = previous_dt + timedelta(microseconds=1)
    return candidate_dt.isoformat()


def _bounded(
    value: str | None, field: str, *, limit: int, pattern: re.Pattern[str] | None = None
) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or not value
        or len(value) > limit
        or any(ord(ch) < 32 for ch in value)
    ):
        raise _error(f"{field} is invalid or exceeds {limit} characters")
    if pattern is not None and pattern.fullmatch(value) is None:
        raise _error(f"{field} has an invalid format")
    return value


def _bounded_multiline(value: str | None, field: str, *, limit: int) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or not value
        or len(value) > limit
        or any(ord(character) < 32 and character not in "\n\t\r" for character in value)
    ):
        raise _error(f"{field} is invalid or exceeds {limit} characters")
    return value


def _looks_high_entropy(value: str) -> bool:
    if len(value) < 32 or len(set(value)) < 12:
        return False
    classes = sum(
        (
            any(character.islower() for character in value),
            any(character.isupper() for character in value),
            any(character.isdigit() for character in value),
            any(not character.isalnum() for character in value),
        )
    )
    return classes >= 3 or (classes >= 2 and len(value) >= 40)


def _sanitize_message(value: str | None) -> str | None:
    if value is None:
        return None
    result = _PRIVATE_KEY.sub("<redacted:private-key>", value)
    result = redact_text(result, limit=max(2_000, len(result)))

    def replace_token(match: re.Match[str]) -> str:
        candidate = match.group(1)
        return "<redacted:high-entropy>" if _looks_high_entropy(candidate) else candidate

    result = _TOKEN_CANDIDATE.sub(replace_token, result)
    if len(result) > 2_000:
        result = result[:2_000]
    return result or None


def validate_operation_id(operation_id: str) -> str:
    value = _bounded(operation_id, "operation_id", limit=27, pattern=_OPERATION_ID)
    assert value is not None
    return value


def _validate_binding(binding: OperationSnapshotBinding | None) -> OperationSnapshotBinding | None:
    if binding is None:
        return None
    head_sha = _bounded(binding.head_sha, "snapshot_binding.head_sha", limit=40, pattern=_SHA40)
    fingerprint = _bounded(
        binding.workspace_fingerprint,
        "snapshot_binding.workspace_fingerprint",
        limit=64,
        pattern=_SHA64,
    )
    generation = binding.config_generation
    if generation is not None and (
        not isinstance(generation, int) or isinstance(generation, bool) or generation < 0
    ):
        raise _error("snapshot_binding.config_generation must be a non-negative integer")
    evidence = _bounded(
        binding.evidence_snapshot_id,
        "snapshot_binding.evidence_snapshot_id",
        limit=128,
        pattern=_SAFE_ID,
    )
    return OperationSnapshotBinding(head_sha, fingerprint, generation, evidence)


def _validate_progress(current: int, total: int | None) -> None:
    if not isinstance(current, int) or isinstance(current, bool) or current < 0:
        raise _error("progress_current must be a non-negative integer")
    if total is not None:
        if not isinstance(total, int) or isinstance(total, bool) or total < 0:
            raise _error("progress_total must be a non-negative integer")
        if current > total:
            raise _error("progress_current cannot exceed progress_total")


def operation_record_inconsistencies(task: OperationTask) -> tuple[str, ...]:
    diagnostics: list[str] = []
    terminal = task.state in TERMINAL_OPERATION_STATES
    if terminal and task.phase != task.state.value:
        diagnostics.append("terminal_phase_mismatch")
    if task.state is OperationState.SUCCEEDED:
        if task.result_reference is None:
            diagnostics.append("missing_result_reference")
        if task.progress_total is not None and task.progress_current != task.progress_total:
            diagnostics.append("terminal_progress_incomplete")
        if task.progress_message != "Completed":
            diagnostics.append("terminal_message_mismatch")
    elif terminal and task.progress_message is not None:
        diagnostics.append("terminal_message_mismatch")
    if not terminal and task.result_reference is not None:
        diagnostics.append("nonterminal_result_reference")
    if task.state in {OperationState.FAILED, OperationState.EXPIRED, OperationState.ORPHANED}:
        if task.error_code is None:
            diagnostics.append("terminal_error_missing")
    elif task.error_code is not None or task.error_message is not None:
        diagnostics.append("unexpected_error_evidence")
    return tuple(sorted(set(diagnostics)))


def normalize_loaded_operation(
    task: OperationTask,
    *,
    source_schema_version: int,
) -> OperationTask:
    observed = operation_record_inconsistencies(task)
    provenance = (
        "legacy_migrated"
        if source_schema_version == LEGACY_OPERATION_SCHEMA_VERSION
        else "recovered_inconsistent"
        if observed
        else task.record_provenance
    )
    diagnostics = tuple(sorted(set(task.record_diagnostics) | set(observed)))
    normalized = replace(
        task,
        phase=task.state.value if task.state in TERMINAL_OPERATION_STATES else task.phase,
        record_provenance=provenance,
        record_consistency="record_inconsistent" if diagnostics else "consistent",
        record_diagnostics=diagnostics,
        schema_version=OPERATION_SCHEMA_VERSION,
    )
    return validate_operation_task(normalized)


def validate_operation_task(task: OperationTask) -> OperationTask:
    if (
        not isinstance(task.schema_version, int)
        or isinstance(task.schema_version, bool)
        or task.schema_version != OPERATION_SCHEMA_VERSION
    ):
        raise _error(
            f"Unsupported operation schema version: {task.schema_version}",
            code=ErrorCode.OPERATION_SCHEMA_UNSUPPORTED,
        )
    validate_operation_id(task.operation_id)
    _bounded(task.kind, "kind", limit=64, pattern=_SAFE_KIND)
    _bounded(task.phase, "phase", limit=64, pattern=_SAFE_PHASE)
    _validate_progress(task.progress_current, task.progress_total)
    _bounded(task.progress_unit, "progress_unit", limit=64)
    _bounded_multiline(task.progress_message, "progress_message", limit=2_000)
    if task.progress_message != _sanitize_message(task.progress_message):
        raise _error("progress_message is not safely redacted")
    _bounded(task.task_id, "task_id", limit=128, pattern=_SAFE_ID)
    _bounded(task.workspace_id, "workspace_id", limit=128, pattern=_SAFE_ID)
    _validate_binding(task.snapshot_binding)
    _bounded(task.result_reference, "result_reference", limit=256, pattern=_SAFE_ID)
    _bounded(task.error_code, "error_code", limit=128, pattern=_SAFE_ID)
    _bounded_multiline(task.error_message, "error_message", limit=2_000)
    if task.error_message != _sanitize_message(task.error_message):
        raise _error("error_message is not safely redacted")
    if not isinstance(task.cancel_supported, bool):
        raise _error("cancel_supported must be a boolean")
    created_at = _parse_timestamp(task.created_at, "created_at")
    updated_at = _parse_timestamp(task.updated_at, "updated_at")
    if updated_at < created_at:
        raise _error("updated_at cannot precede created_at")
    if task.expires_at is not None:
        _parse_timestamp(task.expires_at, "expires_at")
    if task.cancellation_requested_at is not None:
        _parse_timestamp(task.cancellation_requested_at, "cancellation_requested_at")
    _bounded(task.receipt_id, "receipt_id", limit=32, pattern=_RECEIPT_ID)
    if task.record_provenance not in _RECORD_PROVENANCE:
        raise _error("record_provenance is invalid")
    if task.record_consistency not in _RECORD_CONSISTENCY:
        raise _error("record_consistency is invalid")
    if not isinstance(task.record_diagnostics, tuple) or len(task.record_diagnostics) > 20:
        raise _error("record_diagnostics must be a tuple with at most 20 items")
    for item in task.record_diagnostics:
        _bounded(item, "record_diagnostics", limit=128, pattern=_RECORD_DIAGNOSTIC)
    if tuple(sorted(set(task.record_diagnostics))) != task.record_diagnostics:
        raise _error("record_diagnostics must be sorted and unique")
    if task.record_consistency == "record_inconsistent" and not task.record_diagnostics:
        raise _error("record_inconsistent requires diagnostic evidence")
    if task.record_provenance == "current":
        if task.record_consistency != "consistent" or task.record_diagnostics:
            raise _error("current operation records must be consistent")
        inconsistencies = operation_record_inconsistencies(task)
        if inconsistencies:
            raise _error(
                "Current operation record violates lifecycle invariants: "
                + ",".join(inconsistencies),
                code=ErrorCode.OPERATION_TRANSITION_INVALID,
            )
    if task.record_provenance == "recovered_inconsistent" and (
        task.record_consistency != "record_inconsistent"
    ):
        raise _error("recovered_inconsistent provenance requires inconsistent evidence")
    return task


def new_operation_task(
    *,
    operation_id: str,
    kind: str,
    phase: str,
    now: str,
    cancel_supported: bool,
    task_id: str | None = None,
    workspace_id: str | None = None,
    snapshot_binding: OperationSnapshotBinding | None = None,
    expires_at: str | None = None,
) -> OperationTask:
    task = OperationTask(
        operation_id=validate_operation_id(operation_id),
        kind=str(_bounded(kind, "kind", limit=64, pattern=_SAFE_KIND)),
        state=OperationState.PENDING,
        phase=str(_bounded(phase, "phase", limit=64, pattern=_SAFE_PHASE)),
        progress_current=0,
        progress_total=None,
        progress_unit=None,
        progress_message=None,
        task_id=_bounded(task_id, "task_id", limit=128, pattern=_SAFE_ID),
        workspace_id=_bounded(workspace_id, "workspace_id", limit=128, pattern=_SAFE_ID),
        snapshot_binding=_validate_binding(snapshot_binding),
        result_reference=None,
        error_code=None,
        error_message=None,
        retryability=OperationRetryability.NONE,
        cancel_supported=cancel_supported,
        cancellation_requested_at=None,
        created_at=now,
        updated_at=now,
        expires_at=expires_at,
    )
    return validate_operation_task(task)


def transition_operation(
    task: OperationTask,
    new_state: OperationState,
    *,
    now: str,
    result_reference: str | None = None,
    receipt_id: str | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
    retryability: OperationRetryability = OperationRetryability.NONE,
) -> OperationTask:
    validate_operation_task(task)
    if new_state is task.state:
        if (
            any(
                value is not None
                for value in (result_reference, receipt_id, error_code, error_message)
            )
            or retryability is not task.retryability
        ):
            raise _error(
                "An idempotent same-state transition cannot change terminal data",
                code=ErrorCode.OPERATION_TRANSITION_INVALID,
            )
        return task
    if new_state not in _ALLOWED_TRANSITIONS.get(task.state, frozenset()):
        raise _error(
            f"Operation transition is not allowed: {task.state.value} -> {new_state.value}",
            code=ErrorCode.OPERATION_TRANSITION_INVALID,
        )
    safe_error = _sanitize_message(error_message)
    resolved_error_code = error_code
    if resolved_error_code is None:
        resolved_error_code = {
            OperationState.FAILED: "OPERATION_FAILED",
            OperationState.EXPIRED: "OPERATION_EXPIRED",
            OperationState.ORPHANED: "OPERATION_WORKER_LOST",
        }.get(new_state)
    terminal = new_state in TERMINAL_OPERATION_STATES
    progress_current = task.progress_current
    progress_message = task.progress_message
    if new_state is OperationState.SUCCEEDED:
        if task.progress_total is not None:
            progress_current = task.progress_total
        progress_message = "Completed"
    elif terminal:
        progress_message = None
    updated = replace(
        task,
        state=new_state,
        phase=new_state.value,
        progress_current=progress_current,
        progress_message=progress_message,
        result_reference=_bounded(
            result_reference, "result_reference", limit=256, pattern=_SAFE_ID
        ),
        receipt_id=_bounded(
            receipt_id if receipt_id is not None else task.receipt_id,
            "receipt_id",
            limit=32,
            pattern=_RECEIPT_ID,
        ),
        error_code=_bounded(resolved_error_code, "error_code", limit=128, pattern=_SAFE_ID),
        error_message=_bounded_multiline(safe_error, "error_message", limit=2_000),
        retryability=retryability,
        record_consistency="consistent",
        record_diagnostics=(),
        updated_at=next_operation_timestamp(task.updated_at, now),
    )
    if task.record_provenance != "current":
        diagnostics = operation_record_inconsistencies(updated)
        updated = replace(
            updated,
            record_consistency="record_inconsistent" if diagnostics else "consistent",
            record_diagnostics=diagnostics,
        )
    return validate_operation_task(updated)


def update_operation_progress(
    task: OperationTask,
    *,
    phase: str,
    current: int,
    total: int | None = None,
    unit: str | None = None,
    message: str | None = None,
    now: str,
) -> OperationTask:
    validate_operation_task(task)
    if task.state is not OperationState.RUNNING:
        raise _error("Progress may be updated only while an operation is running")
    safe_phase = str(_bounded(phase, "phase", limit=64, pattern=_SAFE_PHASE))
    _validate_progress(current, total)
    if safe_phase == task.phase and current < task.progress_current:
        raise _error("Progress cannot move backwards within one phase")
    safe_message = _sanitize_message(message)
    updated = replace(
        task,
        phase=safe_phase,
        progress_current=current,
        progress_total=total,
        progress_unit=_bounded(unit, "progress_unit", limit=64),
        progress_message=_bounded_multiline(safe_message, "progress_message", limit=2_000),
        updated_at=next_operation_timestamp(task.updated_at, now),
    )
    return validate_operation_task(updated)


def request_operation_cancellation(
    task: OperationTask, *, now: str
) -> OperationCancellationDecision:
    validate_operation_task(task)
    if task.state in TERMINAL_OPERATION_STATES:
        return OperationCancellationDecision(task, False, False, True, task.cancel_supported)
    if not task.cancel_supported:
        return OperationCancellationDecision(task, False, False, False, False)
    if task.cancellation_requested_at is not None:
        return OperationCancellationDecision(task, False, True, False, True)
    updated_at = next_operation_timestamp(task.updated_at, now)
    updated = replace(task, cancellation_requested_at=updated_at, updated_at=updated_at)
    return OperationCancellationDecision(validate_operation_task(updated), True, False, False, True)
