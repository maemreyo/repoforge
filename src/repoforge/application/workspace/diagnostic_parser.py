"""Pure bounded parsers for reviewed diagnostic command results."""

from __future__ import annotations

import re
from dataclasses import dataclass

from ...domain.diagnostics import DiagnosticParserKind, DiagnosticProfileConfig
from ...domain.errors import ErrorCode, RepoForgeError
from ...ports.command import CommandResult

_PYTEST_COUNT = re.compile(r"(?P<count>\d+)\s+(?P<kind>passed|failed|error|errors|skipped)")
_RELEASE_MATCH = re.compile(
    r"release contracts match:\s*(?P<tools>\d+)\s+MCP tools,\s*surface=(?P<surface>[a-f0-9]+),\s*runtime-protocol=(?P<protocol>\d+)"
)


@dataclass(frozen=True, slots=True)
class ParsedDiagnostic:
    outcome: str
    failure_class: str | None
    fields: dict[str, int | str]
    excerpt: str
    output_truncated: bool


def _excerpt(result: CommandResult, *, limit: int = 2_000) -> str:
    text = result.combined
    if len(text) <= limit:
        return text
    half = max(1, limit // 2)
    return f"{text[:half]}\n\n... diagnostic excerpt omitted ...\n\n{text[-half:]}"


def _failure_class(result: CommandResult) -> str:
    rendered = result.combined.lower()
    if "modulenotfounderror" in rendered or "no module named" in rendered:
        return "dependency_missing"
    if "executable not found" in rendered or "command not found" in rendered:
        return "tool_missing"
    if "timed out" in rendered or "timeout" in rendered:
        return "timeout"
    if "permission denied" in rendered or "unsupported platform" in rendered:
        return "environment_mismatch"
    return "diagnostic_failure"


def _parse_pytest(result: CommandResult) -> ParsedDiagnostic:
    counts = {"passed": 0, "failed": 0, "errors": 0, "skipped": 0}
    for match in _PYTEST_COUNT.finditer(result.combined):
        kind = match.group("kind")
        normalized = "errors" if kind in {"error", "errors"} else kind
        counts[normalized] = int(match.group("count"))
    if result.returncode == 0:
        outcome = "passed"
        failure = None
    else:
        outcome = "failed"
        failure = (
            "test_failure"
            if counts["failed"] or counts["errors"] or "failed " in result.combined.lower()
            else _failure_class(result)
        )
    public_counts: dict[str, int | str] = dict(counts)
    return ParsedDiagnostic(
        outcome,
        failure,
        public_counts,
        _excerpt(result),
        result.stdout_truncated or result.stderr_truncated,
    )


def _parse_release_contract(result: CommandResult) -> ParsedDiagnostic:
    match = _RELEASE_MATCH.search(result.combined)
    if result.returncode == 0 and match is None:
        raise RepoForgeError(
            "Release-contract diagnostic returned unrecognized success output",
            code=ErrorCode.DIAGNOSTIC_PARSER_FAILED,
            unchanged_state=("No release contract was updated.",),
        )
    fields: dict[str, int | str] = {}
    if match is not None:
        fields = {
            "tool_count": int(match.group("tools")),
            "surface_hash": match.group("surface"),
            "runtime_protocol": int(match.group("protocol")),
        }
    outcome = "passed" if result.returncode == 0 else "failed"
    failure = (
        None
        if result.returncode == 0
        else (
            "contract_drift"
            if "release contract" in result.combined.lower()
            else _failure_class(result)
        )
    )
    return ParsedDiagnostic(
        outcome,
        failure,
        fields,
        _excerpt(result),
        result.stdout_truncated or result.stderr_truncated,
    )


def _parse_text(result: CommandResult) -> ParsedDiagnostic:
    return ParsedDiagnostic(
        "passed" if result.returncode == 0 else "failed",
        None if result.returncode == 0 else _failure_class(result),
        {},
        _excerpt(result),
        result.stdout_truncated or result.stderr_truncated,
    )


def parse_diagnostic(
    profile: DiagnosticProfileConfig,
    result: CommandResult,
) -> ParsedDiagnostic:
    if profile.parser is DiagnosticParserKind.PYTEST:
        return _parse_pytest(result)
    if profile.parser is DiagnosticParserKind.RELEASE_CONTRACT:
        return _parse_release_contract(result)
    if profile.parser is DiagnosticParserKind.TEXT:
        return _parse_text(result)
    raise RepoForgeError(
        f"Unsupported diagnostic parser: {profile.parser}",
        code=ErrorCode.DIAGNOSTIC_PARSER_FAILED,
    )
