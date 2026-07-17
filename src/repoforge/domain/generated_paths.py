"""Typed declarations for files that must be regenerated rather than hand-merged."""

from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import PurePosixPath
from typing import Any

_MAX_RULES = 64
_MAX_GLOB_CHARS = 512
_MAX_DESCRIPTION_CHARS = 500
_MAX_COMMAND_ARGS = 64
_MAX_ARGUMENT_CHARS = 512


class GeneratedPathError(ValueError):
    """Raised when generated-path policy is malformed or unsafe."""


def _bounded_text(value: object, context: str, *, limit: int, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise GeneratedPathError(f"{context} must be a string")
    normalized = value.strip()
    if (not normalized and not allow_empty) or len(normalized) > limit:
        qualifier = "between 1 and" if not allow_empty else "at most"
        raise GeneratedPathError(f"{context} must contain {qualifier} {limit} characters")
    if any(ord(character) < 32 for character in normalized):
        raise GeneratedPathError(f"{context} contains control characters")
    return normalized


def _safe_glob(value: object, context: str) -> str:
    glob = _bounded_text(value, context, limit=_MAX_GLOB_CHARS).replace("\\", "/")
    candidate = PurePosixPath(glob)
    if (
        glob.startswith(("/", "-", ":"))
        or ".." in candidate.parts
        or any(part in {"", "."} for part in candidate.parts)
    ):
        raise GeneratedPathError(f"{context} must be a safe repository-relative glob")
    return glob


def _command(value: object, context: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or not value or len(value) > _MAX_COMMAND_ARGS:
        raise GeneratedPathError(
            f"{context} must be a non-empty argument array with at most {_MAX_COMMAND_ARGS} entries"
        )
    return tuple(
        _bounded_text(argument, f"{context}[{index}]", limit=_MAX_ARGUMENT_CHARS)
        for index, argument in enumerate(value)
    )


@dataclass(frozen=True, slots=True)
class GeneratedPathRule:
    glob: str
    regeneration_command: tuple[str, ...]
    description: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "glob", _safe_glob(self.glob, "generated path glob"))
        object.__setattr__(
            self,
            "regeneration_command",
            _command(self.regeneration_command, "generated path regeneration_command"),
        )
        object.__setattr__(
            self,
            "description",
            _bounded_text(
                self.description,
                "generated path description",
                limit=_MAX_DESCRIPTION_CHARS,
            ),
        )

    @classmethod
    def from_mapping(cls, raw: object, *, context: str) -> GeneratedPathRule:
        if not isinstance(raw, dict) or set(raw) != {
            "glob",
            "regeneration_command",
            "description",
        }:
            raise GeneratedPathError(
                f"{context} must contain exactly glob, regeneration_command, and description"
            )
        return cls(
            glob=_safe_glob(raw["glob"], f"{context}.glob"),
            regeneration_command=_command(
                raw["regeneration_command"], f"{context}.regeneration_command"
            ),
            description=_bounded_text(
                raw["description"],
                f"{context}.description",
                limit=_MAX_DESCRIPTION_CHARS,
            ),
        )

    def as_table(self) -> dict[str, Any]:
        return {
            "glob": self.glob,
            "regeneration_command": list(self.regeneration_command),
            "description": self.description,
        }

    def matches(self, path: str) -> bool:
        normalized = path.replace("\\", "/").lstrip("./")
        return fnmatchcase(normalized, self.glob)


def parse_generated_paths(raw: object, *, context: str) -> tuple[GeneratedPathRule, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list) or len(raw) > _MAX_RULES:
        raise GeneratedPathError(f"{context} must be an array of at most {_MAX_RULES} entries")
    rules = tuple(
        GeneratedPathRule.from_mapping(item, context=f"{context}[{index}]")
        for index, item in enumerate(raw)
    )
    globs = [rule.glob for rule in rules]
    if len(globs) != len(set(globs)):
        raise GeneratedPathError(f"{context} contains duplicate globs")
    return tuple(sorted(rules, key=lambda rule: rule.glob))


def generated_path_rule_for(
    rules: tuple[GeneratedPathRule, ...], path: str
) -> GeneratedPathRule | None:
    return next((rule for rule in rules if rule.matches(path)), None)
