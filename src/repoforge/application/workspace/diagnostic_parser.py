"""Pure bounded parsers and expectation evaluation for reviewed diagnostic results."""

from __future__ import annotations

import re
from dataclasses import dataclass

from ...domain.diagnostics import (
    DiagnosticExpectation,
    DiagnosticFailureClass,
    DiagnosticParserKind,
    DiagnosticProfileConfig,
)
from ...domain.errors import ErrorCode, RepoForgeError
from ...domain.verification import VerificationIntent
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
    business_tests_ran: bool = False
    failed_selectors: tuple[str, ...] = ()
    output_artifact_reference: str | None = None


@dataclass(frozen=True, slots=True)
class DiagnosticExpectationEvaluation:
    expectation_met: bool | None
    valid_tdd_red_evidence: bool


def _excerpt(result: CommandResult, *, limit: int = 2_000) -> str:
    text = result.combined
    if len(text) <= limit:
        return text
    half = max(1, limit // 2)
    return f"{text[:half]}\n\n... diagnostic excerpt omitted ...\n\n{text[-half:]}"


def _failure_class(result: CommandResult) -> str:
    rendered = result.combined.lower()
    if "syntaxerror" in rendered or "syntax error" in rendered:
        return DiagnosticFailureClass.SYNTAX_ERROR.value
    if "modulenotfounderror" in rendered or "no module named" in rendered:
        return DiagnosticFailureClass.DEPENDENCY_MISSING.value
    if "importerror" in rendered or "cannot import name" in rendered:
        return DiagnosticFailureClass.IMPORT_ERROR.value
    if (
        "error collecting" in rendered
        or "errors during collection" in rendered
        or "no tests ran" in rendered
        or "collected 0 items" in rendered
    ):
        return DiagnosticFailureClass.COLLECTION_ERROR.value
    if "executable not found" in rendered or "command not found" in rendered:
        return DiagnosticFailureClass.TOOL_MISSING.value
    if "timed out" in rendered or "timeout" in rendered:
        return DiagnosticFailureClass.TIMEOUT.value
    if "permission denied" in rendered or "unsupported platform" in rendered:
        return DiagnosticFailureClass.ENVIRONMENT_MISMATCH.value
    return DiagnosticFailureClass.DIAGNOSTIC_FAILURE.value


def _parse_pytest(result: CommandResult) -> ParsedDiagnostic:
    counts = {"passed": 0, "failed": 0, "errors": 0, "skipped": 0}
    for match in _PYTEST_COUNT.finditer(result.combined):
        kind = match.group("kind")
        normalized = "errors" if kind in {"error", "errors"} else kind
        counts[normalized] = int(match.group("count"))
    collected = counts["passed"] + counts["failed"] + counts["skipped"]
    fields: dict[str, int | str] = {**counts, "collected": collected}
    infrastructure_failure = _failure_class(result) if result.returncode != 0 else None
    if result.returncode == 0:
        outcome = "passed"
        failure = None
    else:
        outcome = "failed"
        failure = (
            infrastructure_failure
            if infrastructure_failure
            in {
                DiagnosticFailureClass.COLLECTION_ERROR.value,
                DiagnosticFailureClass.SYNTAX_ERROR.value,
                DiagnosticFailureClass.IMPORT_ERROR.value,
                DiagnosticFailureClass.DEPENDENCY_MISSING.value,
                DiagnosticFailureClass.TOOL_MISSING.value,
                DiagnosticFailureClass.TIMEOUT.value,
                DiagnosticFailureClass.ENVIRONMENT_MISMATCH.value,
            }
            else (
                DiagnosticFailureClass.TEST_FAILURE.value
                if counts["failed"] > 0 or "failed " in result.combined.lower()
                else infrastructure_failure
            )
        )
    business_tests_ran = collected > 0 and failure not in {
        DiagnosticFailureClass.COLLECTION_ERROR.value,
        DiagnosticFailureClass.SYNTAX_ERROR.value,
        DiagnosticFailureClass.IMPORT_ERROR.value,
        DiagnosticFailureClass.DEPENDENCY_MISSING.value,
        DiagnosticFailureClass.TOOL_MISSING.value,
        DiagnosticFailureClass.TIMEOUT.value,
        DiagnosticFailureClass.ENVIRONMENT_MISMATCH.value,
    }
    return ParsedDiagnostic(
        outcome,
        failure,
        fields,
        _excerpt(result),
        result.stdout_truncated or result.stderr_truncated,
        business_tests_ran,
        result.failed_selectors,
        result.output_artifact_reference,
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
            DiagnosticFailureClass.CONTRACT_DRIFT.value
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
        failed_selectors=result.failed_selectors,
        output_artifact_reference=result.output_artifact_reference,
    )


def _parse_text(result: CommandResult) -> ParsedDiagnostic:
    return ParsedDiagnostic(
        "passed" if result.returncode == 0 else "failed",
        None if result.returncode == 0 else _failure_class(result),
        {},
        _excerpt(result),
        result.stdout_truncated or result.stderr_truncated,
        failed_selectors=result.failed_selectors,
        output_artifact_reference=result.output_artifact_reference,
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


def evaluate_diagnostic_expectation(
    parsed: ParsedDiagnostic,
    *,
    intent: VerificationIntent,
    expectation: DiagnosticExpectation,
    expected_failure_class: DiagnosticFailureClass | None,
) -> DiagnosticExpectationEvaluation:
    if expectation is DiagnosticExpectation.NONE:
        expectation_met: bool | None = None
    elif expectation is DiagnosticExpectation.PASS:
        expectation_met = parsed.outcome == "passed" and parsed.business_tests_ran
    else:
        expectation_met = (
            parsed.outcome == "failed"
            and parsed.business_tests_ran
            and (
                expected_failure_class is None
                or parsed.failure_class == expected_failure_class.value
            )
        )
    valid_red = (
        intent is VerificationIntent.TDD_RED
        and expectation is DiagnosticExpectation.FAIL
        and expectation_met is True
        and parsed.failure_class == DiagnosticFailureClass.TEST_FAILURE.value
    )
    return DiagnosticExpectationEvaluation(expectation_met, valid_red)
