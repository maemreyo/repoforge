"""Typed policy for repository-reviewed workspace diagnostics."""

from __future__ import annotations

import re
import string
from collections import Counter
from dataclasses import dataclass
from enum import Enum
from pathlib import PurePosixPath

from .errors import ConfigError

_DIAGNOSTIC_ID = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_SAFE_SELECTOR_VALUE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,127}$")
_SELECTOR_NAME = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_PLACEHOLDER = re.compile(r"\{selector(?::(?P<name>[a-z][a-z0-9_]{0,31}))?\}")

#: Predefined, bounded character classes a template may compose for a ``token``
#: selector. No user-supplied regex is ever accepted -- only allowlisted
#: composition of these fixed sets.
_ALNUM = frozenset(string.ascii_letters + string.digits)
_TOKEN_CHAR_CLASSES: dict[str, frozenset[str]] = {
    "alnum": _ALNUM,
    "underscore": frozenset("_"),
    "path": frozenset("._:/-"),
    "brackets": frozenset("[]"),
    "space": frozenset(" "),
    "at": frozenset("@"),
    "plus": frozenset("+"),
}

#: ``pytest_node`` is a predefined alias of a token class set: alphanumerics
#: plus the path separators/colons pytest node ids require.
PYTEST_NODE_CHAR_CLASSES: tuple[str, ...] = (
    "alnum",
    "underscore",
    "path",
    "brackets",
    "at",
    "plus",
)

_EXPANSION_MODES = ("repeat", "join")
#: Separators a ``join`` expansion may use. Space is intentionally excluded --
#: it would let a joined value be re-split into extra, unreviewed argv tokens
#: by argv-unaware downstream tooling.
_ALLOWED_SEPARATORS = (",", "|", ":", ";")
_MAX_SELECTOR_VALUES = 100
MAX_ARGV_ELEMENTS = 128
_MAX_ARGV_ELEMENTS = MAX_ARGV_ELEMENTS


class DiagnosticSelectorKind(str, Enum):
    NONE = "none"
    TRACKED_PATH = "tracked_path"
    PYTEST_NODE = "pytest_node"
    PACKAGE_NAME = "package_name"
    ENUM = "enum"
    CHECK_ID = "check_id"
    TOKEN = "token"


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
    name: str = "selector"
    values: tuple[str, ...] = ()
    char_classes: tuple[str, ...] = ()
    max_length: int = 128
    prefix: str | None = None
    suffix: str | None = None
    max_values: int = 1
    expansion: str = "repeat"
    separator: str | None = None
    allow_leading_dash: bool = False


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
    selector2: DiagnosticSelectorConfig | None = None

    @property
    def selectors(self) -> tuple[DiagnosticSelectorConfig, ...]:
        """Every configured selector, in placeholder-declaration order."""
        if self.selector2 is None:
            return (self.selector,)
        return (self.selector, self.selector2)


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


def _validate_literal(value: str | None, field: str, *, max_length: int) -> None:
    if value is None:
        return
    if not isinstance(value, str) or not value or len(value) > max_length:
        raise ConfigError(f"{field} must be a non-empty bounded literal string")
    if any(ord(character) < 32 for character in value):
        raise ConfigError(f"{field} contains control characters")


def _validate_selector_shape(
    selector: DiagnosticSelectorConfig, profile_id: str, *, label: str
) -> None:
    if _SELECTOR_NAME.fullmatch(selector.name) is None:
        raise ConfigError(f"diagnostic {profile_id}.{label}.name has an invalid format")
    if (
        not isinstance(selector.max_values, int)
        or isinstance(selector.max_values, bool)
        or not 1 <= selector.max_values <= _MAX_SELECTOR_VALUES
    ):
        raise ConfigError(
            f"diagnostic {profile_id}.{label}.max_values must be between 1 and {_MAX_SELECTOR_VALUES}"
        )
    if selector.expansion not in _EXPANSION_MODES:
        raise ConfigError(
            f"diagnostic {profile_id}.{label}.expansion must be one of {_EXPANSION_MODES}"
        )
    if selector.expansion == "join":
        if selector.max_values <= 1:
            raise ConfigError(
                f"diagnostic {profile_id}.{label}.expansion='join' requires max_values > 1"
            )
        if selector.separator not in _ALLOWED_SEPARATORS:
            raise ConfigError(
                f"diagnostic {profile_id}.{label}.separator must be one of {_ALLOWED_SEPARATORS} for join expansion"
            )
    elif selector.separator is not None:
        raise ConfigError(
            f"diagnostic {profile_id}.{label}.separator is valid only for expansion='join'"
        )
    if selector.max_values > 1 and selector.kind is DiagnosticSelectorKind.NONE:
        raise ConfigError(
            f"diagnostic {profile_id}.{label} cannot use max_values > 1 with kind=none"
        )

    if selector.kind is DiagnosticSelectorKind.TOKEN:
        if not selector.char_classes:
            raise ConfigError(f"diagnostic {profile_id}.{label}.char_classes cannot be empty")
        unknown = sorted(set(selector.char_classes) - set(_TOKEN_CHAR_CLASSES))
        if unknown:
            raise ConfigError(
                f"diagnostic {profile_id}.{label}.char_classes contains unknown classes: {unknown}"
            )
        if len(set(selector.char_classes)) != len(selector.char_classes):
            raise ConfigError(f"diagnostic {profile_id}.{label}.char_classes contains duplicates")
        if (
            not isinstance(selector.max_length, int)
            or isinstance(selector.max_length, bool)
            or not 1 <= selector.max_length <= 512
        ):
            raise ConfigError(
                f"diagnostic {profile_id}.{label}.max_length must be between 1 and 512"
            )
        _validate_literal(selector.prefix, f"diagnostic {profile_id}.{label}.prefix", max_length=64)
        _validate_literal(selector.suffix, f"diagnostic {profile_id}.{label}.suffix", max_length=64)
    else:
        if selector.char_classes:
            raise ConfigError(
                f"diagnostic {profile_id}.{label}.char_classes is valid only for kind='token'"
            )
        if selector.prefix is not None or selector.suffix is not None:
            raise ConfigError(
                f"diagnostic {profile_id}.{label}.prefix/suffix is valid only for kind='token'"
            )

    if selector.kind is DiagnosticSelectorKind.ENUM:
        if not selector.values:
            raise ConfigError(f"diagnostic {profile_id}.{label}.values cannot be empty")
        if len(set(selector.values)) != len(selector.values):
            raise ConfigError(f"diagnostic {profile_id}.{label}.values contains duplicates")
        for value in selector.values:
            if _SAFE_SELECTOR_VALUE.fullmatch(value) is None or value.startswith("-"):
                raise ConfigError(
                    f"diagnostic {profile_id}.{label}.values contains an invalid value"
                )
    elif selector.values:
        raise ConfigError(f"diagnostic {profile_id}.{label}.values is valid only for kind='enum'")

    if selector.allow_leading_dash and selector.kind not in {
        DiagnosticSelectorKind.TOKEN,
        DiagnosticSelectorKind.CHECK_ID,
        DiagnosticSelectorKind.PACKAGE_NAME,
    }:
        raise ConfigError(
            f"diagnostic {profile_id}.{label}.allow_leading_dash is valid only for kind in "
            "{'token', 'check_id', 'package_name'}"
        )


def _placeholder_names(argv_template: tuple[str, ...], profile_id: str) -> list[str]:
    names: list[str] = []
    for argument in argv_template:
        remainder = argument
        for match in _PLACEHOLDER.finditer(argument):
            remainder = remainder.replace(match.group(0), "", 1)
        if "{" in remainder or "}" in remainder:
            raise ConfigError(f"diagnostic {profile_id}.argv contains an unknown placeholder")
        matches = list(_PLACEHOLDER.finditer(argument))
        if matches and argument != matches[0].group(0):
            raise ConfigError(
                f"diagnostic {profile_id}.argv selector placeholder must occupy one complete argv element"
            )
        if len(matches) > 1:
            raise ConfigError(
                f"diagnostic {profile_id}.argv selector placeholder must occupy one complete argv element"
            )
        if matches:
            names.append(matches[0].group("name") or "selector")
    return names


def _placeholder_index(argv_template: tuple[str, ...], name: str) -> int | None:
    for index, argument in enumerate(argv_template):
        match = _PLACEHOLDER.fullmatch(argument)
        if match and (match.group("name") or "selector") == name:
            return index
    return None


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
    if not profile.argv_template or len(profile.argv_template) > _MAX_ARGV_ELEMENTS:
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

    placeholder_names = _placeholder_names(profile.argv_template, profile.diagnostic_id)

    selectors = profile.selectors
    declared_names = [selector.name for selector in selectors]
    if len(declared_names) != len(set(declared_names)):
        raise ConfigError(f"diagnostic {profile.diagnostic_id} declares duplicate selector names")
    if len(selectors) > 2:
        raise ConfigError(f"diagnostic {profile.diagnostic_id} may declare at most two selectors")

    for index, selector in enumerate(selectors):
        _validate_selector_shape(selector, profile.diagnostic_id, label=f"selector[{index}]")

    active_selectors = [s for s in selectors if s.kind is not DiagnosticSelectorKind.NONE]
    active_names = {s.name for s in active_selectors}
    placeholder_counts = Counter(placeholder_names)
    if set(placeholder_counts) != active_names:
        raise ConfigError(
            f"diagnostic {profile.diagnostic_id}.argv placeholders {sorted(placeholder_counts)} "
            f"do not match declared selectors {sorted(active_names)}"
        )
    for selector in active_selectors:
        if placeholder_counts[selector.name] != 1:
            raise ConfigError(
                f"diagnostic {profile.diagnostic_id}.argv must contain exactly one placeholder "
                f"for selector '{selector.name}'"
            )
        if selector.allow_leading_dash:
            placeholder_index = _placeholder_index(profile.argv_template, selector.name)
            if (
                placeholder_index is None
                or placeholder_index == 0
                or profile.argv_template[placeholder_index - 1] != "--"
            ):
                raise ConfigError(
                    f"diagnostic {profile.diagnostic_id}.selector '{selector.name}' sets "
                    "allow_leading_dash but its argv placeholder is not immediately preceded "
                    "by a literal '--' terminator"
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

    # The worst-case fully expanded argv (every multi-value selector at its
    # declared max, 'repeat' expansion) must still respect the argv bound.
    worst_case = len(profile.argv_template)
    for selector in active_selectors:
        if selector.expansion == "repeat" and selector.max_values > 1:
            worst_case += selector.max_values - 1
    if worst_case > _MAX_ARGV_ELEMENTS:
        raise ConfigError(
            f"diagnostic {profile.diagnostic_id} can expand beyond the {_MAX_ARGV_ELEMENTS}-element argv bound"
        )
    return profile


def token_char_classes() -> dict[str, frozenset[str]]:
    """Expose the predefined character-class sets a ``token`` selector may compose."""
    return dict(_TOKEN_CHAR_CLASSES)


def allowed_join_separators() -> tuple[str, ...]:
    return _ALLOWED_SEPARATORS


__all__ = [
    "MAX_ARGV_ELEMENTS",
    "PYTEST_NODE_CHAR_CLASSES",
    "DiagnosticExpectation",
    "DiagnosticFailureClass",
    "DiagnosticMutability",
    "DiagnosticNetworkPolicy",
    "DiagnosticParserKind",
    "DiagnosticProfileConfig",
    "DiagnosticSelectorConfig",
    "DiagnosticSelectorKind",
    "allowed_join_separators",
    "token_char_classes",
    "validate_diagnostic_expectation",
    "validate_diagnostic_profile",
]
