"""Pure selector validation, CI evidence redaction, and failure classification."""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..config import RepositoryConfig
from .egress import (
    EgressContentClass,
    EgressDestination,
    EgressPolicy,
    EgressRange,
    EgressRequest,
    evaluate_egress,
)
from .errors import ErrorCode, RepoForgeError, SecurityError
from .policy import assert_path_allowed

_SELECTOR = re.compile(r"check-run:([1-9][0-9]{0,19})")
_PATH_CANDIDATE = re.compile(
    r"(?:^|[\s'\"(])"
    r"(\.env(?:\.[A-Za-z0-9_.-]+)?|"
    r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+|"
    r"[A-Za-z0-9_.-]+\.(?:pem|key))"
)


@dataclass(frozen=True, slots=True)
class SanitizedCiText:
    text: str
    redacted: bool
    withheld_lines: int
    truncated: bool


@dataclass(frozen=True, slots=True)
class CiFailureClassification:
    failure_class: str
    retryable: bool


def parse_check_selector(value: str) -> int:
    """Return a positive Check Run ID from one opaque RepoForge selector."""
    match = _SELECTOR.fullmatch(value)
    if match is None:
        raise RepoForgeError(
            "Invalid CI check selector; use the exact check-run:<id> value returned by workspace_pr_checks",
            code=ErrorCode.CHECK_SELECTOR_INVALID,
            safe_next_action="Call workspace_pr_checks and reuse an exact returned selector.",
        )
    check_run_id = int(match.group(1))
    if check_run_id > 9_223_372_036_854_775_807:
        raise RepoForgeError(
            "Invalid CI check selector; Check Run ID exceeds the supported integer range",
            code=ErrorCode.CHECK_SELECTOR_INVALID,
            safe_next_action="Call workspace_pr_checks and reuse an exact returned selector.",
        )
    return check_run_id


def _line_exposes_denied_path(line: str, repo: RepositoryConfig) -> bool:
    for match in _PATH_CANDIDATE.finditer(line):
        candidate = match.group(1).rstrip(".,:;)]}")
        try:
            assert_path_allowed(candidate, repo)
        except SecurityError:
            return True
    return False


def _render_egress_ranges(value: str, ranges: tuple[EgressRange, ...]) -> str:
    parts: list[str] = []
    cursor = 0
    for item in ranges:
        parts.append(value[cursor : item.start])
        if "private_key" in item.category:
            parts.append("<redacted:private-key>")
        elif "high_entropy" in item.category:
            parts.append("<redacted:high-entropy>")
        else:
            parts.append("<redacted>")
        cursor = item.end
    parts.append(value[cursor:])
    return "".join(parts)


def sanitize_ci_text(
    value: str,
    repo: RepositoryConfig,
    *,
    max_chars: int,
    secrets: tuple[str, ...] = (),
) -> SanitizedCiText:
    """Apply central egress policy, denied-path withholding, and legacy CI rendering."""

    if not isinstance(max_chars, int) or isinstance(max_chars, bool) or max_chars <= 0:
        raise ValueError("max_chars must be a positive integer")
    encoded_bytes = len(value.encode("utf-8", errors="replace"))
    if encoded_bytes > 20_000_000:
        return SanitizedCiText(
            "<withheld:oversized-ci-evidence>",
            True,
            0,
            True,
        )
    result = evaluate_egress(
        EgressRequest(
            value,
            EgressContentClass.DIAGNOSTIC,
            EgressDestination.MODEL,
            explicit_secrets=secrets,
            policy=EgressPolicy(
                max_input_bytes=max(1, encoded_bytes),
                max_output_chars=max(1, min(max(len(value), max_chars), 1_000_000)),
                max_output_lines=20_000,
                withhold_private_keys=False,
            ),
        )
    )
    egress_sanitized = _render_egress_ranges(value, result.redaction_ranges)
    withheld = 0
    lines: list[str] = []
    for line in egress_sanitized.splitlines():
        if _line_exposes_denied_path(line, repo):
            withheld += 1
            lines.append("<withheld:denied-source-snippet>")
        else:
            lines.append(line)
    sanitized = "\n".join(lines)
    truncated = len(sanitized) > max_chars
    if truncated:
        omitted = len(sanitized) - max_chars
        sanitized = f"{sanitized[:max_chars]}\n... <{omitted} characters omitted>"
    return SanitizedCiText(
        sanitized,
        bool(result.redaction_ranges) or withheld > 0 or sanitized != value,
        withheld,
        truncated,
    )


def classify_ci_failure(
    parts: list[str] | tuple[str, ...],
    *,
    status: str | None = None,
    conclusion: str | None = None,
) -> CiFailureClassification:
    """Classify sanitized CI evidence using deterministic ordered heuristics."""
    normalized_status = (status or "").lower()
    normalized_conclusion = (conclusion or "").lower()
    text = "\n".join(parts).lower()

    if normalized_conclusion in {"success", "neutral"}:
        return CiFailureClassification("pass", False)
    if normalized_conclusion == "skipped":
        return CiFailureClassification("skipped", False)
    if normalized_status not in {"", "completed"} and not normalized_conclusion:
        return CiFailureClassification("pending", False)

    rules: tuple[tuple[str, tuple[str, ...], bool], ...] = (
        ("cancellation", ("cancelled", "canceled", "canceling"), True),
        ("timeout", ("timed out", "timeout", "deadline exceeded"), True),
        ("policy", ("policy denied", "policy violation", "not permitted", "forbidden"), False),
        ("lint", ("ruff", "eslint", "lint failed", "flake8", "pylint"), False),
        ("type", ("mypy", "pyright", "type error", "typecheck", "type check"), False),
        ("build", ("build failed", "compile error", "compilation failed", "wheel failed"), False),
        (
            "dependency",
            ("dependency", "lock resolution", "could not resolve", "package not found"),
            False,
        ),
        (
            "environment",
            ("runner image", "tool missing", "command not found", "environment mismatch"),
            True,
        ),
        (
            "network",
            ("connection reset", "network", "dns", "tls", "service unavailable", "http 5"),
            True,
        ),
        (
            "test",
            ("pytest", "assertionerror", "test failed", "tests failed", "failure summary"),
            False,
        ),
    )
    state_text = f"{normalized_conclusion}\n{text}"
    for failure_class, needles, retryable in rules:
        if any(needle in state_text for needle in needles):
            return CiFailureClassification(failure_class, retryable)
    return CiFailureClassification("unknown", False)
