"""Typed parsing and bounded options for managed CodeGraph enrollments."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from .code_intelligence import MAX_CODE_INTELLIGENCE_FACTS, MAX_CODE_INTELLIGENCE_PATHS
from .errors import ConfigError

_MAX_PROJECTION_FILES = 1_000_000
_MAX_PROJECTION_BYTES = 10 * 1024 * 1024 * 1024
_OPTION_FIELDS = frozenset(
    {
        "init_timeout_seconds",
        "sync_timeout_seconds",
        "query_timeout_seconds",
        "max_changed_paths",
        "max_relationships",
        "max_affected_paths",
        "max_depth",
        "projection_max_files",
        "projection_max_bytes",
        "canary_timeout_seconds",
    }
)


def _bounded(value: int, name: str, minimum: int, maximum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer between {minimum} and {maximum}")
    return value


@dataclass(frozen=True, slots=True)
class CodeGraphOptions:
    """Reviewed operational bounds; repository policy may impose tighter ceilings."""

    init_timeout_seconds: int = 120
    sync_timeout_seconds: int = 60
    query_timeout_seconds: int = 15
    max_changed_paths: int = 256
    max_relationships: int = 1_000
    max_affected_paths: int = 1_000
    max_depth: int = 5
    projection_max_files: int = 20_000
    projection_max_bytes: int = 250_000_000
    canary_timeout_seconds: int = 180

    def __post_init__(self) -> None:
        bounds = (
            (self.init_timeout_seconds, "init_timeout_seconds", 1, 600),
            (self.sync_timeout_seconds, "sync_timeout_seconds", 1, 300),
            (self.query_timeout_seconds, "query_timeout_seconds", 1, 120),
            (self.max_changed_paths, "max_changed_paths", 1, 2_000),
            (
                self.max_relationships,
                "max_relationships",
                1,
                MAX_CODE_INTELLIGENCE_FACTS,
            ),
            (
                self.max_affected_paths,
                "max_affected_paths",
                1,
                MAX_CODE_INTELLIGENCE_PATHS,
            ),
            (self.max_depth, "max_depth", 1, 10),
            (
                self.projection_max_files,
                "projection_max_files",
                1,
                _MAX_PROJECTION_FILES,
            ),
            (
                self.projection_max_bytes,
                "projection_max_bytes",
                1,
                _MAX_PROJECTION_BYTES,
            ),
            (self.canary_timeout_seconds, "canary_timeout_seconds", 1, 600),
        )
        for value, name, minimum, maximum in bounds:
            _bounded(value, name, minimum, maximum)

    def as_dict(self) -> dict[str, int]:
        return {
            "init_timeout_seconds": self.init_timeout_seconds,
            "sync_timeout_seconds": self.sync_timeout_seconds,
            "query_timeout_seconds": self.query_timeout_seconds,
            "max_changed_paths": self.max_changed_paths,
            "max_relationships": self.max_relationships,
            "max_affected_paths": self.max_affected_paths,
            "max_depth": self.max_depth,
            "projection_max_files": self.projection_max_files,
            "projection_max_bytes": self.projection_max_bytes,
            "canary_timeout_seconds": self.canary_timeout_seconds,
        }

    @property
    def options_digest(self) -> str:
        encoded = json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()


def _integer(table: dict[str, object], name: str, default: int, context: str) -> int:
    value = table.get(name, default)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ConfigError(f"{context}.{name} must be an integer")
    return value


def codegraph_options_from_config(raw: object, context: str) -> CodeGraphOptions | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ConfigError(f"{context} must be a TOML table")
    table = {str(key): value for key, value in raw.items()}
    unknown = sorted(set(table) - _OPTION_FIELDS)
    if unknown:
        raise ConfigError(f"{context} contains unsupported CodeGraph options: {unknown}")
    defaults = CodeGraphOptions()
    try:
        return CodeGraphOptions(
            **{
                name: _integer(table, name, default, context)
                for name, default in defaults.as_dict().items()
            }
        )
    except ValueError as exc:
        raise ConfigError(f"{context} is invalid: {exc}") from exc


__all__ = ["CodeGraphOptions", "codegraph_options_from_config"]
