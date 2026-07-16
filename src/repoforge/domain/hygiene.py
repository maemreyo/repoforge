"""Pure policy and evidence model for repository-reviewed hygiene checks."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import PurePosixPath

from .errors import ConfigError

_FORMATTER_ID = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_SHELL_EXECUTABLES = frozenset({"bash", "cmd", "fish", "powershell", "pwsh", "sh", "zsh"})


class HygieneNetworkPolicy(str, Enum):
    LOCAL_ONLY = "local_only"


class HygieneParserKind(str, Enum):
    RUFF_FORMAT = "ruff_format"


def _normalized_relative(value: str, field: str, *, allow_glob: bool = False) -> str:
    if not isinstance(value, str) or not value or len(value) > 512:
        raise ConfigError(f"{field} must be a non-empty bounded repository-relative path")
    if any(ord(character) < 32 for character in value):
        raise ConfigError(f"{field} contains control characters")
    normalized = value.replace("\\", "/").rstrip("/")
    path = PurePosixPath(normalized)
    if (
        not normalized
        or normalized.startswith("/")
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ConfigError(f"{field} must be a normalized repository-relative path")
    if not allow_glob and any(character in normalized for character in "*?[]"):
        raise ConfigError(f"{field} cannot contain glob characters")
    return normalized


def _normalized_text(value: str, field: str, *, maximum: int = 512) -> str:
    if not isinstance(value, str) or any(ord(character) < 32 for character in value):
        raise ConfigError(f"{field} is invalid")
    normalized = " ".join(value.split())
    if not normalized or len(normalized) > maximum:
        raise ConfigError(f"{field} must be non-empty and at most {maximum} characters")
    return normalized


def _validate_argv(
    formatter_id: str,
    name: str,
    argv: tuple[str, ...],
) -> tuple[str, ...]:
    if not argv or len(argv) > 32:
        raise ConfigError(
            f"formatter {formatter_id}.{name} must be a non-empty bounded string array"
        )
    normalized: list[str] = []
    for argument in argv:
        if (
            not isinstance(argument, str)
            or not argument
            or len(argument) > 512
            or any(ord(character) < 32 for character in argument)
        ):
            raise ConfigError(f"formatter {formatter_id}.{name} contains an invalid argument")
        if "{" in argument or "}" in argument:
            raise ConfigError(
                f"formatter {formatter_id}.{name} cannot contain path or shell placeholders"
            )
        normalized.append(argument)
    executable = PurePosixPath(normalized[0].replace("\\", "/")).name.lower()
    if executable in _SHELL_EXECUTABLES:
        raise ConfigError(f"formatter {formatter_id}.{name} cannot invoke a shell executable")
    return tuple(normalized)


@dataclass(frozen=True, order=True, slots=True)
class HygieneFinding:
    path: str
    rule: str
    message: str

    @classmethod
    def create(cls, path: str, rule: str, message: str) -> HygieneFinding:
        return cls(
            _normalized_relative(path, "hygiene finding path"),
            _normalized_text(rule, "hygiene finding rule", maximum=128),
            _normalized_text(message, "hygiene finding message"),
        )

    @property
    def identity(self) -> str:
        payload = json.dumps(
            {"message": self.message, "path": self.path, "rule": self.rule},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class HygieneComparison:
    preexisting: tuple[HygieneFinding, ...]
    introduced: tuple[HygieneFinding, ...]
    resolved: tuple[HygieneFinding, ...]
    changed_path_findings: tuple[HygieneFinding, ...]


def compare_hygiene_findings(
    *,
    base: tuple[HygieneFinding, ...],
    workspace: tuple[HygieneFinding, ...],
    changed_paths: tuple[str, ...],
) -> HygieneComparison:
    base_set = set(base)
    workspace_set = set(workspace)
    normalized_changed = {_normalized_relative(path, "changed path") for path in changed_paths}
    introduced = tuple(sorted(workspace_set - base_set))
    return HygieneComparison(
        preexisting=tuple(sorted(base_set & workspace_set)),
        introduced=introduced,
        resolved=tuple(sorted(base_set - workspace_set)),
        changed_path_findings=tuple(
            finding for finding in introduced if finding.path in normalized_changed
        ),
    )


@dataclass(frozen=True, slots=True)
class FormatterPolicy:
    formatter_id: str
    summary: str
    check_argv: tuple[str, ...]
    fix_argv: tuple[str, ...]
    include_globs: tuple[str, ...]
    timeout_seconds: int
    output_limit: int
    max_paths: int
    baseline_cache_ttl_seconds: int
    network_policy: HygieneNetworkPolicy
    parser: HygieneParserKind

    def __post_init__(self) -> None:
        if _FORMATTER_ID.fullmatch(self.formatter_id) is None:
            raise ConfigError(f"formatter_id has an invalid format: {self.formatter_id!r}")
        object.__setattr__(
            self,
            "summary",
            _normalized_text(self.summary, f"formatter {self.formatter_id}.summary", maximum=256),
        )
        object.__setattr__(
            self,
            "check_argv",
            _validate_argv(self.formatter_id, "check_argv", self.check_argv),
        )
        object.__setattr__(
            self,
            "fix_argv",
            _validate_argv(self.formatter_id, "fix_argv", self.fix_argv),
        )
        if not self.include_globs or len(self.include_globs) > 64:
            raise ConfigError(
                f"formatter {self.formatter_id}.include_globs must be a non-empty bounded array"
            )
        normalized_globs = tuple(
            _normalized_relative(
                pattern,
                f"formatter {self.formatter_id}.include_globs",
                allow_glob=True,
            )
            for pattern in self.include_globs
        )
        if len(set(normalized_globs)) != len(normalized_globs):
            raise ConfigError(f"formatter {self.formatter_id}.include_globs contains duplicates")
        object.__setattr__(self, "include_globs", normalized_globs)
        for field_name, value, minimum, maximum in (
            ("timeout_seconds", self.timeout_seconds, 1, 3_600),
            ("output_limit", self.output_limit, 1, 120_000),
            ("max_paths", self.max_paths, 1, 1_000),
            (
                "baseline_cache_ttl_seconds",
                self.baseline_cache_ttl_seconds,
                60,
                86_400,
            ),
        ):
            if (
                not isinstance(value, int)
                or isinstance(value, bool)
                or not minimum <= value <= maximum
            ):
                raise ConfigError(
                    f"formatter {self.formatter_id}.{field_name} must be between {minimum} and {maximum}"
                )
        if not isinstance(self.network_policy, HygieneNetworkPolicy):
            raise ConfigError(f"formatter {self.formatter_id}.network_policy is invalid")
        if not isinstance(self.parser, HygieneParserKind):
            raise ConfigError(f"formatter {self.formatter_id}.parser is invalid")

    @property
    def contract_hash(self) -> str:
        payload = {
            "baseline_cache_ttl_seconds": self.baseline_cache_ttl_seconds,
            "check_argv": list(self.check_argv),
            "fix_argv": list(self.fix_argv),
            "formatter_id": self.formatter_id,
            "include_globs": list(self.include_globs),
            "max_paths": self.max_paths,
            "network_policy": self.network_policy.value,
            "output_limit": self.output_limit,
            "parser": self.parser.value,
            "timeout_seconds": self.timeout_seconds,
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()


__all__ = [
    "FormatterPolicy",
    "HygieneComparison",
    "HygieneFinding",
    "HygieneNetworkPolicy",
    "HygieneParserKind",
    "compare_hygiene_findings",
]
