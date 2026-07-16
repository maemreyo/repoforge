"""Pure policy for the audited ad-hoc command runner (per-repository relaxed execution mode).

Relaxed execution mode is an escape valve for trusted local repositories when no
enrolled diagnostic template fits: an agent may run an exact allowlisted-runner
command, but the result is evidence only and never satisfies the
verification-before-commit gate. See ``src/repoforge/application/workspace/commit.py``
for the exact-tree fingerprint gate this must never influence.
"""

from __future__ import annotations

import re
from enum import Enum

from .errors import ConfigError, ErrorCode, RepoForgeError

_RUNNER_BASENAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

MAX_ADHOC_RUNNERS = 32
MAX_ADHOC_ARGV_ELEMENTS = 32
MAX_ADHOC_ARGV_ELEMENT_LENGTH = 512


class ExecutionMode(str, Enum):
    STRICT = "strict"
    RELAXED = "relaxed"


def validate_adhoc_runners(runners: tuple[str, ...], repo_id: str) -> tuple[str, ...]:
    """Validate a repository's ``adhoc_runners`` allowlist at config-load time."""
    if len(runners) > MAX_ADHOC_RUNNERS:
        raise ConfigError(
            f"repositories.{repo_id}.adhoc_runners must not exceed {MAX_ADHOC_RUNNERS} entries"
        )
    if len(set(runners)) != len(runners):
        raise ConfigError(f"repositories.{repo_id}.adhoc_runners contains duplicates")
    for runner in runners:
        if (
            not isinstance(runner, str)
            or _RUNNER_BASENAME.fullmatch(runner) is None
            or "/" in runner
            or "\\" in runner
        ):
            raise ConfigError(
                f"repositories.{repo_id}.adhoc_runners contains an invalid runner basename: {runner!r}"
            )
    return runners


def _adhoc_error(message: str, code: ErrorCode, *, safe_next_action: str) -> RepoForgeError:
    return RepoForgeError(
        message,
        code=code,
        unchanged_state=("The workspace, configuration, and remote state were not modified.",),
        safe_next_action=safe_next_action,
    )


def validate_adhoc_argv(argv: tuple[str, ...], runners: tuple[str, ...]) -> tuple[str, ...]:
    """Validate one ad-hoc argv list against the repository's runner allowlist.

    Never accepts a shell string, a path-form ``argv[0]``, or an unlisted runner.
    Raises a structured :class:`RepoForgeError` on any violation.
    """
    if not isinstance(argv, (list, tuple)) or not argv or len(argv) > MAX_ADHOC_ARGV_ELEMENTS:
        raise _adhoc_error(
            f"Ad-hoc argv must be a non-empty list of at most {MAX_ADHOC_ARGV_ELEMENTS} elements",
            ErrorCode.ADHOC_ARGV_INVALID,
            safe_next_action="Supply a bounded argv list; split a longer command into multiple ad-hoc runs.",
        )
    for element in argv:
        if (
            not isinstance(element, str)
            or not element
            or len(element) > MAX_ADHOC_ARGV_ELEMENT_LENGTH
            or "\x00" in element
            or any(ord(character) < 32 for character in element)
        ):
            raise _adhoc_error(
                "Ad-hoc argv contains an empty, oversized, or control-character element",
                ErrorCode.ADHOC_ARGV_INVALID,
                safe_next_action=(
                    f"Keep every argv element non-empty, printable, and at most "
                    f"{MAX_ADHOC_ARGV_ELEMENT_LENGTH} characters."
                ),
            )
    runner = argv[0]
    if "/" in runner or "\\" in runner:
        raise _adhoc_error(
            f"Ad-hoc argv[0] must be a bare executable name, not a path: {runner!r}",
            ErrorCode.ADHOC_RUNNER_NOT_ALLOWED,
            safe_next_action=(
                "Pass a bare runner basename (e.g. 'uv', 'pytest', 'node') as argv[0]; "
                "RepoForge resolves it through the constrained runtime PATH."
            ),
        )
    if runner not in runners:
        raise _adhoc_error(
            f"Ad-hoc runner {runner!r} is not in this repository's adhoc_runners allowlist",
            ErrorCode.ADHOC_RUNNER_NOT_ALLOWED,
            safe_next_action=(
                "Use one of the repository's configured adhoc_runners, or ask the repository "
                "owner to add this runner to repositories.<repo_id>.adhoc_runners."
            ),
        )
    return tuple(argv)


__all__ = [
    "MAX_ADHOC_ARGV_ELEMENTS",
    "MAX_ADHOC_ARGV_ELEMENT_LENGTH",
    "MAX_ADHOC_RUNNERS",
    "ExecutionMode",
    "validate_adhoc_argv",
    "validate_adhoc_runners",
]
