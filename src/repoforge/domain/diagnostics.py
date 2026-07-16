"""Typed policy for repository-reviewed workspace diagnostics."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from pathlib import PurePosixPath

from .errors import ConfigError

_DIAGNOSTIC_ID = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_SAFE_SELECTOR_VALUE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,127}$")


class DiagnosticSelectorKind(str, Enum):
    NONE = "none"
    TRACKED_PATH = "tracked_path"
    PYTEST_NODE = "pytest_node"
    PACKAGE_NAME = "package_name"
    ENUM = "enum"
    CHECK_ID = "check_id"


class DiagnosticNetworkPolicy(str, Enum):
    LOCAL_ONLY = "local_only"


class DiagnosticMutability(str, Enum):
    READ_ONLY = "read_only"
    ARTIFACTS = "artifacts"


class DiagnosticParserKind(str, Enum):
    PYTEST = "pytest"
    RELEASE_CONTRACT = "release_contract"
    TEXT = "text"


class DiagnosticExpectation(str, Enum):
    NONE = "none"
    PASS = "pass"
    FAIL = "fail"

    @classmethod
    def parse(cls, value: DiagnosticExpectation | str | None) -> DiagnosticExpectation:
        if value is None:
            return cls.NONE
        if isinstance(value, cls):
            return value
        try:
            return cls(value)
        except ValueError as exc:
            raise ConfigError(
                f"Unknown diagnostic expectation {value!r}. Available: {[item.value for item in cls]}"
            ) from exc


class DiagnosticFailureClass(str, Enum):
    TEST_FAILURE = "test_failure"
    COLLECTION_ERROR = "collection_error"
    SYNTAX_ERROR = "syntax_error"
    IMPORT_ERROR = "import_error"
    DEPENDENCY_MISSING = "dependency_missing"
    TOOL_MISSING = "tool_missing"
    TIMEOUT = "timeout"
    ENVIRONMENT_MISMATCH = "environment_mismatch"
    CONTRACT_DRIFT = "contract_drift"
    DIAGNOSTIC_FAILURE = "diagnostic_failure"

    @classmethod
    def parse_optional(
        cls, value: DiagnosticFailureClass | str | None
    ) -> DiagnosticFailureClass | None:
        if value is None:
            return None
        if isinstance(value, cls):
            return value
        try:
            return cls(value)
        except ValueError as exc:
            raise ConfigError(
                f"Unknown diagnostic failure class {value!r}. Available: {[item.value for item in cls]}"
            ) from exc


@dataclass(frozen=True, slots=True)
class DiagnosticSelectorConfig:
    kind: DiagnosticSelectorKind
    values: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class DiagnosticProfileConfig:
    diagnostic_id: str
    summary: str
    argv_template: tuple[str, ...]
    selector: DiagnosticSelectorConfig
    working_directory: str | None
    timeout_seconds: int
    network_policy: DiagnosticNetworkPolicy
    mutability: DiagnosticMutability
    parser: DiagnosticParserKind
    output_limit: int
    artifact_paths: tuple[str, ...] = ()


def validate_diagnostic_expectation(
    expectation: DiagnosticExpectation | str | None,
    expected_failure_class: DiagnosticFailureClass | str | None,
) -> tuple[DiagnosticExpectation, DiagnosticFailureClass | None]:
    normalized_expectation = DiagnosticExpectation.parse(expectation)
    normalized_failure = DiagnosticFailureClass.parse_optional(expected_failure_class)
    if normalized_failure is not None and normalized_expectation is not DiagnosticExpectation.FAIL:
        raise ConfigError("expected_failure_class requires expectation='fail'")
    return normalized_expectation, normalized_failure


def _safe_relative(value: str, field: str, *, allow_glob: bool = False) -> str:
    if not isinstance(value, str) or not value or len(value) > 256:
        raise ConfigError(f"{field} must be a non-empty bounded relative path")
    if any(ord(character) < 32 for character in value):
        raise ConfigError(f"{field} contains control characters")
    raw = value.replace("\\", "/")
    if raw.startswith("/"):
        raise ConfigError(f"{field} must be a normalized repository-relative path")
    normalized = raw.rstrip("/")
    path = PurePosixPath(normalized)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ConfigError(f"{field} must be a normalized repository-relative path")
    if not allow_glob and any(character in normalized for character in "*?[]"):
        raise ConfigError(f"{field} cannot contain glob characters")
    return normalized


def validate_diagnostic_profile(profile: DiagnosticProfileConfig) -> DiagnosticProfileConfig:
    if _DIAGNOSTIC_ID.fullmatch(profile.diagnostic_id) is None:
        raise ConfigError(f"diagnostic_id has an invalid format: {profile.diagnostic_id!r}")
    if (
        not isinstance(profile.summary, str)
        or not profile.summary.strip()
        or len(profile.summary) > 256
        or any(ord(character) < 32 for character in profile.summary)
    ):
        raise ConfigError(f"diagnostic {profile.diagnostic_id}.summary is invalid")
    if not profile.argv_template or len(profile.argv_template) > 32:
        raise ConfigError(
            f"diagnostic {profile.diagnostic_id}.argv must be a non-empty bounded array"
        )
    for argument in profile.argv_template:
        if (
            not isinstance(argument, str)
            or not argument
            or len(argument) > 512
            or any(ord(character) < 32 for character in argument)
        ):
            raise ConfigError(
                f"diagnostic {profile.diagnostic_id}.argv contains an invalid argument"
            )
        remainder = argument.replace("{selector}", "")
        if "{" in remainder or "}" in remainder:
            raise ConfigError(
                f"diagnostic {profile.diagnostic_id}.argv contains an unknown placeholder"
            )
    placeholder_count = sum(argument.count("{selector}") for argument in profile.argv_template)
    if any(
        "{selector}" in argument and argument != "{selector}" for argument in profile.argv_template
    ):
        raise ConfigError(
            f"diagnostic {profile.diagnostic_id}.argv selector placeholder must occupy one complete argv element"
        )
    if profile.selector.kind is DiagnosticSelectorKind.NONE:
        if placeholder_count:
            raise ConfigError(
                f"diagnostic {profile.diagnostic_id}.argv cannot use a selector placeholder"
            )
    elif placeholder_count != 1:
        raise ConfigError(
            f"diagnostic {profile.diagnostic_id}.argv must contain exactly one selector placeholder"
        )
    if profile.selector.kind is DiagnosticSelectorKind.ENUM:
        if not profile.selector.values:
            raise ConfigError(f"diagnostic {profile.diagnostic_id}.selector_values cannot be empty")
        if len(set(profile.selector.values)) != len(profile.selector.values):
            raise ConfigError(
                f"diagnostic {profile.diagnostic_id}.selector_values contains duplicates"
            )
        for value in profile.selector.values:
            if _SAFE_SELECTOR_VALUE.fullmatch(value) is None or value.startswith("-"):
                raise ConfigError(
                    f"diagnostic {profile.diagnostic_id}.selector_values contains an invalid value"
                )
    elif profile.selector.values:
        raise ConfigError(
            f"diagnostic {profile.diagnostic_id}.selector_values is valid only for enum selectors"
        )
    if profile.working_directory is not None:
        _safe_relative(
            profile.working_directory,
            f"diagnostic {profile.diagnostic_id}.working_directory",
        )
    if (
        not isinstance(profile.timeout_seconds, int)
        or isinstance(profile.timeout_seconds, bool)
        or not 1 <= profile.timeout_seconds <= 3_600
    ):
        raise ConfigError(
            f"diagnostic {profile.diagnostic_id}.timeout_seconds must be between 1 and 3600"
        )
    if (
        not isinstance(profile.output_limit, int)
        or isinstance(profile.output_limit, bool)
        or not 1 <= profile.output_limit <= 120_000
    ):
        raise ConfigError(
            f"diagnostic {profile.diagnostic_id}.output_limit must be between 1 and 120000"
        )
    if profile.mutability is DiagnosticMutability.ARTIFACTS and not profile.artifact_paths:
        raise ConfigError(f"diagnostic {profile.diagnostic_id}.artifact_paths is required")
    if profile.mutability is DiagnosticMutability.READ_ONLY and profile.artifact_paths:
        raise ConfigError(
            f"diagnostic {profile.diagnostic_id}.artifact_paths requires mutability='artifacts'"
        )
    for pattern in profile.artifact_paths:
        _safe_relative(
            pattern,
            f"diagnostic {profile.diagnostic_id}.artifact_paths",
            allow_glob=True,
        )
    return profile


__all__ = [
    "DiagnosticExpectation",
    "DiagnosticFailureClass",
    "DiagnosticMutability",
    "DiagnosticNetworkPolicy",
    "DiagnosticParserKind",
    "DiagnosticProfileConfig",
    "DiagnosticSelectorConfig",
    "DiagnosticSelectorKind",
    "validate_diagnostic_expectation",
    "validate_diagnostic_profile",
]
