"""Pure derivation and matching for command-source integrity evidence (issue #170).

A verification profile's *command-source paths* are the tracked files that define
its command chain (a Makefile a step invokes, a script path named verbatim in
argv). Stamping whether any of them differ from the workspace base at run time
makes the observed "redefine `make check`, run `full`, revert" pattern visible in
evidence and audit instead of silently polluting verification statistics -- this
module never blocks anything, it only classifies.
"""

from __future__ import annotations

import fnmatch
from pathlib import PurePosixPath

from .errors import ConfigError

#: Conservative default command-source files for any profile step invoking `make`.
MAKE_DEFAULT_FILES: tuple[str, ...] = ("Makefile", "makefile", "GNUmakefile")
#: Script-like extensions a verbatim repo-relative argv token is assumed to name.
_SCRIPT_EXTENSIONS = (
    ".py",
    ".sh",
    ".bash",
    ".js",
    ".mjs",
    ".ts",
    ".rb",
    ".pl",
    ".ps1",
)
MAX_COMMAND_SOURCE_PATHS = 20
MAX_COMMAND_SOURCE_PATH_LENGTH = 256
MAX_COMMAND_SOURCE_DIRTY_PATHS_REPORTED = 20


def _invokes_make(argument: str) -> bool:
    basename = argument.rsplit("/", 1)[-1]
    return basename == "make"


def _looks_like_repo_relative_script(argument: str) -> bool:
    if not argument or argument.startswith(("-", "/")):
        return False
    if ".." in argument.split("/"):
        return False
    return argument.endswith(_SCRIPT_EXTENSIONS)


def derive_command_source_paths(commands: tuple[tuple[str, ...], ...]) -> tuple[str, ...]:
    """Conservatively derive command-source paths from a profile's argv steps.

    Any step invoking `make` includes the default Makefile spellings; any argv
    token shaped like a repo-relative script path is included verbatim. This is
    a syntactic heuristic only -- no transitive include-tracking, no shell
    parsing, no filesystem access (profiles are validated before any workspace
    exists).
    """
    derived: set[str] = set()
    for command in commands:
        if not command:
            continue
        if _invokes_make(command[0]):
            derived.update(MAKE_DEFAULT_FILES)
        for argument in command:
            if _looks_like_repo_relative_script(argument):
                derived.add(argument)
    return tuple(sorted(derived))


def validate_command_source_paths(paths: tuple[str, ...], context: str) -> tuple[str, ...]:
    """Validate an explicit or derived command-source path/glob list at config-load time."""
    if len(paths) > MAX_COMMAND_SOURCE_PATHS:
        raise ConfigError(f"{context} must not exceed {MAX_COMMAND_SOURCE_PATHS} entries")
    if len(set(paths)) != len(paths):
        raise ConfigError(f"{context} contains duplicates")
    for pattern in paths:
        if (
            not isinstance(pattern, str)
            or not pattern
            or len(pattern) > MAX_COMMAND_SOURCE_PATH_LENGTH
        ):
            raise ConfigError(f"{context} entries must be non-empty bounded strings")
        if any(ord(character) < 32 for character in pattern):
            raise ConfigError(f"{context} entries cannot contain control characters")
        raw = pattern.replace("\\", "/")
        if raw.startswith("/"):
            raise ConfigError(f"{context} entries must be repository-relative")
        path = PurePosixPath(raw)
        if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
            raise ConfigError(f"{context} entries must be normalized repository-relative paths")
    return paths


def dirty_command_source_paths(
    changed_paths: frozenset[str], declared_paths: tuple[str, ...]
) -> tuple[str, ...]:
    """Return the bounded, sorted subset of `changed_paths` matching a declared pattern."""
    if not declared_paths or not changed_paths:
        return ()
    matched = sorted(
        path
        for path in changed_paths
        if any(
            fnmatch.fnmatchcase(path, pattern) or PurePosixPath(path).match(pattern)
            for pattern in declared_paths
        )
    )
    return tuple(matched[:MAX_COMMAND_SOURCE_DIRTY_PATHS_REPORTED])


__all__ = [
    "MAKE_DEFAULT_FILES",
    "MAX_COMMAND_SOURCE_DIRTY_PATHS_REPORTED",
    "MAX_COMMAND_SOURCE_PATHS",
    "MAX_COMMAND_SOURCE_PATH_LENGTH",
    "derive_command_source_paths",
    "dirty_command_source_paths",
    "validate_command_source_paths",
]
