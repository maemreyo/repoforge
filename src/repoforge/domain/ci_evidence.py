"""Pure selector validation, CI evidence redaction, and failure classification."""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..config import RepositoryConfig
from .errors import ErrorCode, RepoForgeError, SecurityError
from .policy import assert_path_allowed
from .redaction import redact_text

_SELECTOR = re.compile(r"check-run:([1-9][0-9]{0,19})")
_PRIVATE_KEY = re.compile(
    r"-----BEGIN [^-\r\n]*PRIVATE KEY-----.*?-----END [^-\r\n]*PRIVATE KEY-----",
    re.DOTALL,
)
_TOKEN_CANDIDATE = re.compile(r"(?<![A-Za-z0-9])([A-Za-z0-9_./+=-]{32,})(?![A-Za-z0-9])")
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


def _redact_entropy(value: str) -> tuple[str, bool]:
    changed = False

    def replace(match: re.Match[str]) -> str:
        nonlocal changed
        candidate = match.group(1)
        if not _looks_high_entropy(candidate):
            return candidate
        changed = True
        return "<redacted:high-entropy>"

    return _TOKEN_CANDIDATE.sub(replace, value), changed


def _line_exposes_denied_path(line: str, repo: RepositoryConfig) -> bool:
    for match in _PATH_CANDIDATE.finditer(line):
        candidate = match.group(1).rstrip(".,:;)]}")
        try:
            assert_path_allowed(candidate, repo)
        except SecurityError:
            return True
    return False


def sanitize_ci_text(
    value: str,
    repo: RepositoryConfig,
    *,
    max_chars: int,
    secrets: tuple[str, ...] = (),
) -> SanitizedCiText:
    """Redact secrets, withhold denied paths, and bound model-visible CI text."""
    private_redacted, private_count = _PRIVATE_KEY.subn("<redacted:private-key>", value)
    credential_redacted = redact_text(
        private_redacted,
        secrets=secrets,
        limit=max(len(private_redacted), max_chars),
    )
    entropy_redacted, entropy_changed = _redact_entropy(credential_redacted)
    withheld = 0
    lines: list[str] = []
    for line in entropy_redacted.splitlines():
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
        private_count > 0 or entropy_changed or sanitized != value,
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
