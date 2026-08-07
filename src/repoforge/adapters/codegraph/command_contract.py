"""Pure validation and environment contract for CodeGraph CLI execution."""

from __future__ import annotations

import stat
from dataclasses import dataclass
from pathlib import Path

from ...domain.codegraph_config import CodeGraphOptions
from ...domain.errors import ErrorCode, RepoForgeError, SecurityError
from ...domain.policy import normalize_relative_path

_MAX_ARGUMENT_CHARS = 512
_MAX_QUERY_LIMIT = 1_000


@dataclass(frozen=True, slots=True)
class CodeGraphCommandOutput:
    command: str
    stdout: str
    truncated: bool = False


def unavailable(message: str) -> RepoForgeError:
    return RepoForgeError(
        message,
        code=ErrorCode.CODE_INTELLIGENCE_UNAVAILABLE,
        safe_next_action=(
            "Keep baseline code intelligence active, repair the reviewed CodeGraph enrollment, "
            "and retry semantic enrichment."
        ),
        unchanged_state=("No CodeGraph output was accepted as semantic evidence.",),
    )


def prepare_roots(projection_root: Path, home_root: Path) -> tuple[Path, Path]:
    projection = _directory(projection_root, "projection root")
    home = _directory(home_root, "provider home")
    for name in ("cache", "config", "data"):
        _ensure_child_directory(home / name)
    return projection, home


def command_environment(home: Path) -> dict[str, str]:
    return {
        "CI": "1",
        "CODEGRAPH_DIR": ".index",
        "CODEGRAPH_NO_DAEMON": "1",
        "CODEGRAPH_NO_DOWNLOAD": "1",
        "CODEGRAPH_NO_UPDATE_CHECK": "1",
        "CODEGRAPH_TELEMETRY": "0",
        "DO_NOT_TRACK": "1",
        "HOME": str(home),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "NO_COLOR": "1",
        "XDG_CACHE_HOME": str(home / "cache"),
        "XDG_CONFIG_HOME": str(home / "config"),
        "XDG_DATA_HOME": str(home / "data"),
    }


def affected_paths(paths: tuple[str, ...], options: CodeGraphOptions) -> tuple[str, ...]:
    if not isinstance(paths, tuple) or not paths:
        raise ValueError("CodeGraph affected paths must be a non-empty tuple")
    if len(paths) > options.max_changed_paths:
        raise ValueError("CodeGraph affected paths exceed the reviewed bound")
    try:
        normalized = tuple(dict.fromkeys(normalize_relative_path(path) for path in paths))
    except SecurityError as exc:
        raise ValueError("CodeGraph affected path is invalid") from exc
    if any(path.startswith("-") for path in normalized):
        raise ValueError("CodeGraph affected paths must not be option-like")
    return normalized


def graph_depth(value: int | None, options: CodeGraphOptions) -> int:
    depth = options.max_depth if value is None else value
    if not isinstance(depth, int) or isinstance(depth, bool) or not 1 <= depth <= options.max_depth:
        raise ValueError(f"CodeGraph depth must be between 1 and {options.max_depth}")
    return depth


def result_limit(value: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= _MAX_QUERY_LIMIT:
        raise ValueError(f"CodeGraph result limit must be between 1 and {_MAX_QUERY_LIMIT}")
    return value


def argument(value: str, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"CodeGraph {field_name} must be text")
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > _MAX_ARGUMENT_CHARS
        or normalized.startswith("-")
        or any(ord(character) < 32 for character in normalized)
    ):
        raise ValueError(f"CodeGraph {field_name} has an invalid format")
    return normalized


def _directory(path: Path, field_name: str) -> Path:
    expanded = path.expanduser()
    if not expanded.is_absolute() or ".." in expanded.parts:
        raise ValueError(f"CodeGraph {field_name} must be an absolute managed path")

    for component in (*reversed(expanded.parents), expanded):
        try:
            metadata = component.lstat()
        except (FileNotFoundError, NotADirectoryError) as exc:
            raise ValueError(f"CodeGraph {field_name} must be an existing directory") from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise ValueError(f"CodeGraph {field_name} must not contain symlinks")
        if not stat.S_ISDIR(metadata.st_mode):
            raise ValueError(f"CodeGraph {field_name} must be an existing directory")

    return expanded.resolve(strict=True)


def _ensure_child_directory(path: Path) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        path.mkdir(mode=0o700)
        return
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ValueError(f"Managed CodeGraph home child is unsafe: {path.name}")


__all__ = [
    "CodeGraphCommandOutput",
    "affected_paths",
    "argument",
    "command_environment",
    "graph_depth",
    "prepare_roots",
    "result_limit",
    "unavailable",
]
