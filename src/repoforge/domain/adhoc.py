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


class CommandClass(str, Enum):
    """How an ad-hoc command relates to the workspace tree/history.

    Inferred by content inspection for ``git`` only; other runners are opaque and
    return ``None`` from :func:`classify_adhoc_command` so the caller's declared
    mutability governs the exact-state lock.
    """

    READ_ONLY = "read_only"
    MUTATING = "mutating"


# git subcommands that never change the working tree, index, or current-branch HEAD.
# ``fetch`` only updates remote-tracking refs, so the workspace fingerprint and HEAD
# are unaffected; it is read-only for lock purposes even though it touches the network.
_GIT_READ_ONLY_SUBCOMMANDS = frozenset(
    {
        "status",
        "log",
        "diff",
        "show",
        "rev-parse",
        "rev-list",
        "describe",
        "cat-file",
        "ls-files",
        "ls-tree",
        "ls-remote",
        "blame",
        "shortlog",
        "whatchanged",
        "grep",
        "name-rev",
        "for-each-ref",
        "show-ref",
        "merge-base",
        "diff-tree",
        "count-objects",
        "verify-commit",
        "fsck",
        "var",
        "version",
        "help",
        "fetch",
    }
)

# Subcommands whose plain (listing/reading) form is read-only but that mutate when a
# specific token is present. Read-only unless one of the listed tokens appears.
_GIT_MUTATING_TOKENS: dict[str, frozenset[str]] = {
    "branch": frozenset(
        {
            "-d",
            "-D",
            "-m",
            "-M",
            "-c",
            "-C",
            "--delete",
            "--move",
            "--copy",
            "--edit-description",
            "--set-upstream-to",
            "--unset-upstream",
            "-f",
            "--force",
        }
    ),
    "remote": frozenset(
        {"add", "remove", "rm", "rename", "set-url", "set-head", "prune", "update"}
    ),
    "symbolic-ref": frozenset({"-d", "--delete"}),
    "config": frozenset(
        {"--unset", "--unset-all", "--add", "--replace-all", "--edit", "-e", "--set"}
    ),
}

# git subcommands that rewrite history or delete refs/objects irreversibly. These are
# never runnable through the reviewed ad-hoc runner; reviewed remote/history operations
# belong to the typed tools (workspace_push, workspace_refresh).
_GIT_BLOCKED_SUBCOMMANDS = frozenset({"filter-branch", "filter-repo"})

# git global options that consume the following argv element as their value, so the
# subcommand scanner must skip that value when looking for the subcommand token.
_GIT_GLOBAL_VALUE_OPTIONS = frozenset(
    {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--super-prefix", "--exec-path"}
)

_FORCE_WITH_LEASE_EXACT = re.compile(r"^--force-with-lease=[^:\s]+:[0-9a-fA-F]{7,64}$")


def _git_subcommand(git_args: tuple[str, ...]) -> tuple[str | None, tuple[str, ...]]:
    """Return ``(subcommand, remaining_args)`` from the tokens after ``git``.

    Skips leading git global options (including the value of value-taking options in
    separate-argument form) so ``git -C sub -c a.b=c status`` resolves to ``status``.
    """
    index = 0
    while index < len(git_args):
        token = git_args[index]
        if not token.startswith("-"):
            return token, git_args[index + 1 :]
        if token in _GIT_GLOBAL_VALUE_OPTIONS:
            # Separate-argument form consumes the next element (unless it used `opt=value`).
            index += 2
            continue
        index += 1
    return None, ()


def _has_short_flag(args: tuple[str, ...], letter: str) -> bool:
    """True if any clustered short-flag token (e.g. ``-fd``) contains ``letter``."""
    return any(
        token.startswith("-") and not token.startswith("--") and letter in token[1:]
        for token in args
    )


def _assert_git_command_allowed(subcommand: str, rest: tuple[str, ...]) -> CommandClass:
    """Block irreversible/history-rewriting git forms and classify the rest.

    Raises :class:`RepoForgeError` (``ADHOC_COMMAND_FORBIDDEN``) on a blocked form.
    """

    def blocked(reason: str) -> RepoForgeError:
        return _adhoc_error(
            f"git {subcommand}: {reason}",
            ErrorCode.ADHOC_COMMAND_FORBIDDEN,
            safe_next_action=(
                "This irreversible or history-rewriting form is blocked. Use the reviewed typed "
                "tools (workspace_push for pushing, workspace_refresh for base integration) or ask "
                "the operator to perform it directly."
            ),
        )

    if subcommand in _GIT_BLOCKED_SUBCOMMANDS:
        raise blocked("history rewriting is not permitted through the ad-hoc runner")
    # Arbitrary command execution via --exec/-x (rebase, push receive-pack, etc.).
    if any(token == "--exec" or token.startswith("--exec=") for token in rest):
        raise blocked("--exec runs arbitrary commands and is not permitted")
    if subcommand == "rebase" and "-x" in rest:
        raise blocked("rebase -x runs arbitrary commands and is not permitted")
    if subcommand == "push":
        if any(
            token in {"--force", "-f", "--force-if-includes", "--mirror", "--delete", "-d"}
            for token in rest
        ):
            raise blocked(
                "force, mirror, and delete pushes are not permitted; only "
                "--force-with-lease=<ref>:<sha> is allowed"
            )
        for token in rest:
            if token == "--force-with-lease":
                raise blocked(
                    "bare --force-with-lease is not exact-state bound; use "
                    "--force-with-lease=<ref>:<sha>"
                )
            if token.startswith("--force-with-lease=") and not _FORCE_WITH_LEASE_EXACT.match(token):
                raise blocked(
                    "--force-with-lease must be the exact --force-with-lease=<ref>:<sha> form"
                )
    if subcommand == "reflog" and any(token in {"expire", "delete"} for token in rest):
        raise blocked("reflog expire/delete destroys recovery history and is not permitted")
    if subcommand == "update-ref" and any(token in {"-d", "--delete"} for token in rest):
        raise blocked("update-ref delete removes refs directly and is not permitted")
    if subcommand == "clean" and ("--force" in rest or _has_short_flag(rest, "f")):
        raise blocked("git clean --force irreversibly deletes untracked files")

    if subcommand in _GIT_READ_ONLY_SUBCOMMANDS:
        return CommandClass.READ_ONLY
    mutating_tokens = _GIT_MUTATING_TOKENS.get(subcommand)
    if mutating_tokens is not None:
        if subcommand == "config":
            reads = any(token.startswith("--get") or token in {"--list", "-l"} for token in rest)
            is_read = reads and not any(token in mutating_tokens for token in rest)
            return CommandClass.READ_ONLY if is_read else CommandClass.MUTATING
        return (
            CommandClass.MUTATING
            if any(token in mutating_tokens for token in rest)
            else CommandClass.READ_ONLY
        )
    return CommandClass.MUTATING


def classify_adhoc_command(argv: tuple[str, ...]) -> CommandClass | None:
    """Content-inspect one validated ad-hoc argv, blocking irreversible git forms.

    Returns the inferred :class:`CommandClass` for ``git`` commands (raising on a
    blocked form), or ``None`` for other runners whose content RepoForge does not
    inspect (their mutability is governed by the caller's declared intent).
    """
    if not argv or argv[0] != "git":
        return None
    subcommand, rest = _git_subcommand(tuple(argv[1:]))
    if subcommand is None:
        return None
    return _assert_git_command_allowed(subcommand, rest)


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
    "CommandClass",
    "ExecutionMode",
    "classify_adhoc_command",
    "validate_adhoc_argv",
    "validate_adhoc_runners",
]
