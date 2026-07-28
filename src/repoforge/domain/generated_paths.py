"""Typed declarations for files that must be regenerated rather than hand-merged."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath
from typing import Any

_MAX_RULES = 64
_MAX_GLOB_CHARS = 512
_MAX_DESCRIPTION_CHARS = 500
_MAX_COMMAND_ARGS = 64
_MAX_ARGUMENT_CHARS = 512
_MAX_RECEIPTS = 64
_MAX_RECEIPT_PATHS = 1_100
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_GIT_OBJECT_ID = re.compile(r"^(?:[a-f0-9]{40}|[a-f0-9]{64})$")


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


def _receipt_path(value: object) -> str:
    path = _safe_glob(value, "regeneration receipt path")
    if any(marker in path for marker in ("*", "?", "[")):
        raise GeneratedPathError("regeneration receipt path must name one concrete file")
    return path


def generated_paths_identity(root: Path, paths: tuple[str, ...]) -> str | None:
    """Hash exact generated-file identities without trusting receipt-provided digests."""
    try:
        resolved_root = root.resolve(strict=True)
    except OSError:
        return None
    entries: list[dict[str, str]] = []
    try:
        normalized_paths = tuple(sorted({_receipt_path(path) for path in paths}))
    except GeneratedPathError:
        return None
    for path in normalized_paths:
        candidate = resolved_root / path
        if candidate.is_symlink():
            return None
        try:
            resolved = candidate.resolve(strict=False)
            resolved.relative_to(resolved_root)
        except (OSError, ValueError):
            return None
        if not resolved.exists():
            entries.append({"path": path, "state": "missing"})
            continue
        if not resolved.is_file():
            return None
        try:
            digest = hashlib.sha256(resolved.read_bytes()).hexdigest()
        except OSError:
            return None
        entries.append({"path": path, "state": "present", "sha256": digest})
    payload = {"binding": {}, "paths": entries}
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def valid_regenerated_paths(
    root: Path,
    rules: tuple[GeneratedPathRule, ...],
    raw_receipts: object,
) -> frozenset[str]:
    """Return only paths covered by well-formed, declaration-matching, current receipts."""
    if not isinstance(raw_receipts, (list, tuple)) or len(raw_receipts) > _MAX_RECEIPTS:
        return frozenset()
    valid: set[str] = set()
    for raw in raw_receipts:
        if not isinstance(raw, Mapping) or raw.get("schema_version") != 1:
            continue
        if raw.get("deterministic") is not True:
            continue
        output_identity = raw.get("output_identity")
        source_identity = raw.get("source_identity")
        plan_hash = raw.get("plan_hash")
        refresh_commit_sha = raw.get("refresh_commit_sha")
        target_base_sha = raw.get("target_base_sha")
        if (
            not isinstance(output_identity, str)
            or _SHA256.fullmatch(output_identity) is None
            or not isinstance(source_identity, str)
            or _SHA256.fullmatch(source_identity) is None
            or not isinstance(plan_hash, str)
            or _SHA256.fullmatch(plan_hash) is None
            or not isinstance(refresh_commit_sha, str)
            or _GIT_OBJECT_ID.fullmatch(refresh_commit_sha) is None
            or not isinstance(target_base_sha, str)
            or _GIT_OBJECT_ID.fullmatch(target_base_sha) is None
        ):
            continue
        raw_paths = raw.get("generated_paths")
        raw_commands = raw.get("commands")
        if (
            not isinstance(raw_paths, (list, tuple))
            or not 1 <= len(raw_paths) <= _MAX_RECEIPT_PATHS
            or not isinstance(raw_commands, (list, tuple))
            or not 1 <= len(raw_commands) <= _MAX_RULES
        ):
            continue
        try:
            paths = tuple(sorted({_receipt_path(path) for path in raw_paths}))
            commands = frozenset(
                _command(command, f"regeneration receipt commands[{index}]")
                for index, command in enumerate(raw_commands)
            )
        except GeneratedPathError:
            continue
        if any(
            (rule := generated_path_rule_for(rules, path)) is None
            or rule.regeneration_command not in commands
            for path in paths
        ):
            continue
        if generated_paths_identity(root, paths) != output_identity:
            continue
        valid.update(paths)
    return frozenset(valid)
