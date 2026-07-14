"""Typed durable state for exact-SHA pull-request check watches."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum

from .errors import ErrorCode, RepoForgeError
from .operation_task import next_operation_timestamp, validate_operation_id

PR_CHECK_WATCH_SCHEMA_VERSION = 1
_MAX_CHECKS = 200
_MAX_FAILURE_REFERENCES = 20
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SAFE_BRANCH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,255}$")
_SHA40 = re.compile(r"^[a-f0-9]{40}$")
_SHA64 = re.compile(r"^[a-f0-9]{64}$")
_SELECTOR = re.compile(r"^check-run:[1-9][0-9]{0,19}$")
_SAFE_ERROR = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")


class PrCheckWatchUntil(str, Enum):
    ALL_COMPLETED = "all_completed"
    FIRST_FAILURE = "first_failure"


class PrCheckWatchOutcome(str, Enum):
    PENDING = "pending"
    ALL_COMPLETED = "all_completed"
    FIRST_FAILURE = "first_failure"
    CANCELLED = "cancelled"
    FAILED = "failed"
    TIMED_OUT = "timed_out"


TERMINAL_PR_CHECK_WATCH_OUTCOMES = frozenset(
    {
        PrCheckWatchOutcome.ALL_COMPLETED,
        PrCheckWatchOutcome.FIRST_FAILURE,
        PrCheckWatchOutcome.CANCELLED,
        PrCheckWatchOutcome.FAILED,
        PrCheckWatchOutcome.TIMED_OUT,
    }
)


@dataclass(frozen=True, slots=True)
class PrCheckWatch:
    operation_id: str
    workspace_id: str
    branch: str
    pr_number: int
    pushed_sha: str
    workspace_fingerprint: str
    until: PrCheckWatchUntil
    include_failure_evidence: bool
    timeout_seconds: int
    poll_count: int
    pass_count: int
    fail_count: int
    pending_count: int
    skipping_count: int
    selectors: tuple[str, ...]
    failed_selectors: tuple[str, ...]
    evidence_references: tuple[str, ...]
    next_delay_seconds: int
    provider_error_code: str | None
    outcome: PrCheckWatchOutcome
    created_at: str
    updated_at: str
    deadline_at: str
    schema_version: int = PR_CHECK_WATCH_SCHEMA_VERSION


def _error(
    message: str,
    *,
    code: ErrorCode = ErrorCode.PR_CHECK_WATCH_INVALID,
    retryable: bool = False,
) -> RepoForgeError:
    return RepoForgeError(
        message,
        code=code,
        retryable=retryable,
        safe_next_action=(
            "Read the latest operation and workspace state before starting or resuming the watch."
        ),
    )


def _timestamp(value: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise _error(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise _error(f"{field} must include a timezone offset")
    return parsed


def _safe_string(value: str, field: str, pattern: re.Pattern[str]) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise _error(f"{field} has an invalid format")
    return value


def _bounded_int(value: int, field: str, *, minimum: int, maximum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
        raise _error(f"{field} must be between {minimum} and {maximum}")
    return value


def _selectors(values: tuple[str, ...], field: str, *, limit: int) -> tuple[str, ...]:
    if not isinstance(values, tuple):
        raise _error(f"{field} must be a tuple")
    normalized = tuple(sorted(set(values)))
    if len(normalized) > limit:
        raise _error(f"{field} exceeds the maximum of {limit}")
    for value in normalized:
        _safe_string(value, field, _SELECTOR)
    return normalized


def validate_pr_check_watch(watch: PrCheckWatch) -> PrCheckWatch:
    if watch.schema_version != PR_CHECK_WATCH_SCHEMA_VERSION or isinstance(
        watch.schema_version, bool
    ):
        raise _error(
            f"Unsupported PR check watch schema version: {watch.schema_version!r}",
            code=ErrorCode.PR_CHECK_WATCH_STATE_CORRUPT,
        )
    validate_operation_id(watch.operation_id)
    _safe_string(watch.workspace_id, "workspace_id", _SAFE_ID)
    branch = _safe_string(watch.branch, "branch", _SAFE_BRANCH)
    if ".." in branch or branch.startswith("-") or branch.endswith("/") or "//" in branch:
        raise _error("branch is unsafe")
    _bounded_int(watch.pr_number, "pr_number", minimum=1, maximum=2_147_483_647)
    _safe_string(watch.pushed_sha, "pushed_sha", _SHA40)
    _safe_string(watch.workspace_fingerprint, "workspace_fingerprint", _SHA64)
    if not isinstance(watch.until, PrCheckWatchUntil):
        raise _error("until is invalid")
    if not isinstance(watch.include_failure_evidence, bool):
        raise _error("include_failure_evidence must be a boolean")
    _bounded_int(watch.timeout_seconds, "timeout_seconds", minimum=5, maximum=7_200)
    _bounded_int(watch.poll_count, "poll_count", minimum=0, maximum=1_000_000)
    counts = (
        watch.pass_count,
        watch.fail_count,
        watch.pending_count,
        watch.skipping_count,
    )
    for name, value in zip(
        ("pass_count", "fail_count", "pending_count", "skipping_count"),
        counts,
        strict=True,
    ):
        _bounded_int(value, name, minimum=0, maximum=_MAX_CHECKS)
    if sum(counts) > _MAX_CHECKS:
        raise _error("check counts exceed the maximum of 200")
    selectors = _selectors(watch.selectors, "selectors", limit=_MAX_CHECKS)
    failed = _selectors(
        watch.failed_selectors,
        "failed_selectors",
        limit=_MAX_FAILURE_REFERENCES,
    )
    references = _selectors(
        watch.evidence_references,
        "evidence_references",
        limit=_MAX_FAILURE_REFERENCES,
    )
    if not set(failed).issubset(selectors):
        raise _error("failed_selectors must be included in selectors")
    if not set(references).issubset(failed):
        raise _error("evidence_references must be included in failed_selectors")
    _bounded_int(
        watch.next_delay_seconds,
        "next_delay_seconds",
        minimum=1,
        maximum=30,
    )
    if watch.provider_error_code is not None:
        _safe_string(watch.provider_error_code, "provider_error_code", _SAFE_ERROR)
    if not isinstance(watch.outcome, PrCheckWatchOutcome):
        raise _error("outcome is invalid")
    created = _timestamp(watch.created_at, "created_at")
    updated = _timestamp(watch.updated_at, "updated_at")
    deadline = _timestamp(watch.deadline_at, "deadline_at")
    if updated < created:
        raise _error("updated_at cannot precede created_at")
    if deadline <= created:
        raise _error("deadline_at must be later than created_at")
    if watch.outcome is PrCheckWatchOutcome.FIRST_FAILURE and watch.fail_count == 0:
        raise _error("first_failure outcome requires at least one failed check")
    if watch.outcome is PrCheckWatchOutcome.ALL_COMPLETED and watch.pending_count != 0:
        raise _error("all_completed outcome cannot retain pending checks")
    return replace(
        watch,
        selectors=selectors,
        failed_selectors=failed,
        evidence_references=references,
    )


def new_pr_check_watch(
    *,
    operation_id: str,
    workspace_id: str,
    branch: str,
    pr_number: int,
    pushed_sha: str,
    workspace_fingerprint: str,
    until: PrCheckWatchUntil,
    include_failure_evidence: bool,
    timeout_seconds: int,
    created_at: str,
    deadline_at: str,
) -> PrCheckWatch:
    return validate_pr_check_watch(
        PrCheckWatch(
            operation_id=operation_id,
            workspace_id=workspace_id,
            branch=branch,
            pr_number=pr_number,
            pushed_sha=pushed_sha,
            workspace_fingerprint=workspace_fingerprint,
            until=until,
            include_failure_evidence=include_failure_evidence,
            timeout_seconds=timeout_seconds,
            poll_count=0,
            pass_count=0,
            fail_count=0,
            pending_count=0,
            skipping_count=0,
            selectors=(),
            failed_selectors=(),
            evidence_references=(),
            next_delay_seconds=1,
            provider_error_code=None,
            outcome=PrCheckWatchOutcome.PENDING,
            created_at=created_at,
            updated_at=created_at,
            deadline_at=deadline_at,
        )
    )


def update_pr_check_watch(
    watch: PrCheckWatch,
    *,
    now: str,
    poll_count: int,
    pass_count: int,
    fail_count: int,
    pending_count: int,
    skipping_count: int,
    selectors: tuple[str, ...],
    failed_selectors: tuple[str, ...],
    evidence_references: tuple[str, ...],
    next_delay_seconds: int,
    provider_error_code: str | None,
    outcome: PrCheckWatchOutcome,
) -> PrCheckWatch:
    validate_pr_check_watch(watch)
    if watch.outcome in TERMINAL_PR_CHECK_WATCH_OUTCOMES:
        if outcome is watch.outcome:
            return watch
        raise _error("terminal PR check watch state cannot transition")
    if poll_count < watch.poll_count:
        raise _error("poll_count cannot move backwards")
    return validate_pr_check_watch(
        replace(
            watch,
            poll_count=poll_count,
            pass_count=pass_count,
            fail_count=fail_count,
            pending_count=pending_count,
            skipping_count=skipping_count,
            selectors=selectors,
            failed_selectors=failed_selectors,
            evidence_references=evidence_references,
            next_delay_seconds=next_delay_seconds,
            provider_error_code=provider_error_code,
            outcome=outcome,
            updated_at=next_operation_timestamp(watch.updated_at, now),
        )
    )
