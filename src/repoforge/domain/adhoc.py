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
# 100 matches the documented `workspace_verify` collection bound and the
# CommandEvidence argv cap: a full pytest matrix (runner + ~45+ node IDs) must
# fit in one call. Element length stays far tighter (512) and every guard on the
# run (exact-state lock, fingerprint, output cap, timeout) is count-agnostic.
MAX_ADHOC_ARGV_ELEMENTS = 100
MAX_ADHOC_ARGV_ELEMENT_LENGTH = 512
# Standard input is content, not a command argument, so it is bounded far more loosely
# than an argv element -- a patch fed to `git apply -` is routinely longer than 512
# characters. It is still bounded: the text is persisted with the durable work request.
MAX_ADHOC_STDIN_LENGTH = 64_000
# A reviewed shell-script body (#377) is content like stdin, not an argv element -- same
# bound, same rationale.
MAX_ADHOC_SCRIPT_LENGTH = 64_000
# Bounded ordered argv-command sequence (#443): each element is validated exactly like a
# single ad-hoc argv; this only bounds how many can run in one call.
MAX_ADHOC_SEQUENCE_LENGTH = 8


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


def validate_adhoc_runners(
    runners: tuple[str, ...], repo_id: str, *, field_name: str = "adhoc_runners"
) -> tuple[str, ...]:
    """Validate a repository's runner allowlist at config-load time.

    Shared by ``adhoc_runners`` (argv form) and ``adhoc_shell_runners`` (#377's
    reviewed shell-script form) -- both are bare-basename allowlists with the same
    shape and limits; ``field_name`` only changes which field an error names.
    """
    if len(runners) > MAX_ADHOC_RUNNERS:
        raise ConfigError(
            f"repositories.{repo_id}.{field_name} must not exceed {MAX_ADHOC_RUNNERS} entries"
        )
    if len(set(runners)) != len(runners):
        raise ConfigError(f"repositories.{repo_id}.{field_name} contains duplicates")
    for runner in runners:
        if (
            not isinstance(runner, str)
            or _RUNNER_BASENAME.fullmatch(runner) is None
            or "/" in runner
            or "\\" in runner
        ):
            raise ConfigError(
                f"repositories.{repo_id}.{field_name} contains an invalid runner basename: {runner!r}"
            )
    return runners


def validate_adhoc_stdin(text: str | None) -> str | None:
    """Validate optional standard input for one ad-hoc run.

    Unlike an argv element this may contain newlines -- feeding a multi-line patch or
    JSON document is the whole point -- but it is still bounded and must be real text,
    because a NUL byte cannot survive the round trip through the durable work request.
    """
    if text is None:
        return None
    if not isinstance(text, str):
        raise _adhoc_error(
            f"Ad-hoc stdin_text must be a string, got {type(text).__name__}",
            ErrorCode.ADHOC_ARGV_INVALID,
            safe_next_action="Pass stdin_text as text, or omit it to give the command no input.",
        )
    if len(text) > MAX_ADHOC_STDIN_LENGTH:
        raise _adhoc_error(
            f"Ad-hoc stdin_text is {len(text)} characters; the limit is {MAX_ADHOC_STDIN_LENGTH}",
            ErrorCode.ADHOC_ARGV_INVALID,
            safe_next_action=(
                "Write input this large to a file in the workspace and have the command read "
                f"that path; stdin_text carries at most {MAX_ADHOC_STDIN_LENGTH} characters."
            ),
        )
    if "\x00" in text:
        raise _adhoc_error(
            "Ad-hoc stdin_text contains a NUL byte",
            ErrorCode.ADHOC_ARGV_INVALID,
            safe_next_action=(
                "Remove the NUL byte. Binary input belongs in a file the command reads, not in "
                "stdin_text."
            ),
        )
    return text


def validate_adhoc_shell_runner(shell: str, runners: tuple[str, ...]) -> str:
    """Validate the requested interpreter for the reviewed shell-script form (#377).

    Deliberately a separate allowlist from ``adhoc_runners``, empty by default: enabling
    this is the same trust decision as allowlisting a shell interpreter in
    ``adhoc_runners`` already is. ``classify_adhoc_command`` only content-inspects when
    argv[0] == "git" literally, so a script body was never structurally protected from
    reaching git either way -- this does not remove a safety guarantee that existed.
    """
    if not runners:
        raise _adhoc_error(
            "This repository has no adhoc_shell_runners configured; the reviewed "
            "shell-script execution form is disabled",
            ErrorCode.ADHOC_RUNNER_NOT_ALLOWED,
            safe_next_action=(
                "Ask the repository owner to set repositories.<repo_id>.adhoc_shell_runners "
                "(e.g. ['sh', 'bash']), or use the argv form instead."
            ),
        )
    if (
        not isinstance(shell, str)
        or _RUNNER_BASENAME.fullmatch(shell) is None
        or "/" in shell
        or "\\" in shell
    ):
        raise _adhoc_error(
            f"Ad-hoc shell must be a bare executable name, not a path: {shell!r}",
            ErrorCode.ADHOC_RUNNER_NOT_ALLOWED,
            safe_next_action="Pass a bare interpreter basename (e.g. 'sh', 'bash') as shell.",
        )
    if shell not in runners:
        raise _adhoc_error(
            f"Ad-hoc shell {shell!r} is not in this repository's adhoc_shell_runners allowlist",
            ErrorCode.ADHOC_RUNNER_NOT_ALLOWED,
            safe_next_action=(
                "Use one of the repository's configured adhoc_shell_runners, or ask the "
                "repository owner to add this interpreter to "
                "repositories.<repo_id>.adhoc_shell_runners."
            ),
        )
    return shell


def validate_adhoc_script(script: str) -> str:
    """Validate one reviewed shell-script body (#377).

    Bounded like stdin content, not like an argv element: newlines are the whole point.
    Unlike argv, this is never content-inspected for git forms -- see
    :func:`validate_adhoc_shell_runner`.
    """
    if not isinstance(script, str):
        raise _adhoc_error(
            f"Ad-hoc script must be a string, got {type(script).__name__}",
            ErrorCode.ADHOC_ARGV_INVALID,
            safe_next_action="Pass script as text.",
        )
    if not script.strip():
        raise _adhoc_error(
            "Ad-hoc script is empty",
            ErrorCode.ADHOC_ARGV_INVALID,
            safe_next_action="Provide a non-empty script body, or use the argv form instead.",
        )
    if len(script) > MAX_ADHOC_SCRIPT_LENGTH:
        raise _adhoc_error(
            f"Ad-hoc script is {len(script)} characters; the limit is {MAX_ADHOC_SCRIPT_LENGTH}",
            ErrorCode.ADHOC_ARGV_INVALID,
            safe_next_action=(
                "Write a script this large to a file in the workspace and run the "
                f"interpreter against that path instead; script carries at most "
                f"{MAX_ADHOC_SCRIPT_LENGTH} characters."
            ),
        )
    if "\x00" in script:
        raise _adhoc_error(
            "Ad-hoc script contains a NUL byte",
            ErrorCode.ADHOC_ARGV_INVALID,
            safe_next_action="Remove the NUL byte; a shell script cannot contain one.",
        )
    return script


def validate_adhoc_sequence(
    sequence: tuple[tuple[str, ...], ...], runners: tuple[str, ...]
) -> tuple[tuple[str, ...], ...]:
    """Validate a bounded ordered argv-command sequence (#443).

    Every element is validated exactly like a single ad-hoc argv (same allowlist, same
    per-element shape rules) -- this only adds the sequence-length bound. Validating all
    elements up front, before any of them run, means a sequence either runs entirely
    reviewed or not at all; it never starts on element 1 only to discover element 3 was
    invalid.
    """
    if not isinstance(sequence, (list, tuple)) or not sequence:
        raise _adhoc_error(
            "Ad-hoc argv_sequence must be a non-empty list of argv commands",
            ErrorCode.ADHOC_ARGV_INVALID,
            safe_next_action="Supply at least one argv command, or use the single-command argv form.",
        )
    if len(sequence) > MAX_ADHOC_SEQUENCE_LENGTH:
        raise _adhoc_error(
            f"Ad-hoc argv_sequence has {len(sequence)} commands; the limit is "
            f"{MAX_ADHOC_SEQUENCE_LENGTH}",
            ErrorCode.ADHOC_ARGV_INVALID,
            safe_next_action=(
                f"Split this into multiple workspace_exec calls; a sequence carries at most "
                f"{MAX_ADHOC_SEQUENCE_LENGTH} commands."
            ),
        )
    return tuple(validate_adhoc_argv(tuple(element), runners) for element in sequence)


def _adhoc_error(message: str, code: ErrorCode, *, safe_next_action: str) -> RepoForgeError:
    return RepoForgeError(
        message,
        code=code,
        unchanged_state=("The workspace, configuration, and remote state were not modified.",),
        safe_next_action=safe_next_action,
    )


def _argv_element_violation(element: object) -> tuple[str, str] | None:
    """Name why one argv element is unacceptable, or ``None`` when it is fine.

    One message per cause. A single message covering "empty, oversized, or
    control-character" forces the caller to re-derive which of the three it hit, and a
    newline in particular reads as neither of the other two.
    """
    if not isinstance(element, str):
        return (
            f"must be a string, got {type(element).__name__}",
            "Pass each argv element as a separate string; RepoForge never accepts a shell string.",
        )
    if not element:
        return (
            "is empty",
            "Drop the empty element; an argv element must carry an actual token.",
        )
    if len(element) > MAX_ADHOC_ARGV_ELEMENT_LENGTH:
        return (
            f"is {len(element)} characters; the limit is {MAX_ADHOC_ARGV_ELEMENT_LENGTH}",
            f"Keep every argv element at most {MAX_ADHOC_ARGV_ELEMENT_LENGTH} characters. A "
            "program too long to pass inline belongs in a file the runner reads.",
        )
    if "\n" in element or "\r" in element:
        return (
            "contains a newline",
            "Ad-hoc argv carries one command, not a script: pass single-line arguments, or put "
            "a multi-line program in a file and pass that path.",
        )
    if "\x00" in element:
        return (
            "contains a NUL byte",
            "Remove the NUL byte; it cannot be passed to a process argument.",
        )
    for character in element:
        if ord(character) < 32:
            return (
                f"contains the control character U+{ord(character):04X}",
                "Keep every argv element printable.",
            )
    return None


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
    for index, element in enumerate(argv):
        violation = _argv_element_violation(element)
        if violation is not None:
            reason, remedy = violation
            raise _adhoc_error(
                f"Ad-hoc argv[{index}] {reason}",
                ErrorCode.ADHOC_ARGV_INVALID,
                safe_next_action=remedy,
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
    "MAX_ADHOC_SCRIPT_LENGTH",
    "MAX_ADHOC_SEQUENCE_LENGTH",
    "MAX_ADHOC_STDIN_LENGTH",
    "CommandClass",
    "ExecutionMode",
    "classify_adhoc_command",
    "validate_adhoc_argv",
    "validate_adhoc_runners",
    "validate_adhoc_script",
    "validate_adhoc_sequence",
    "validate_adhoc_shell_runner",
    "validate_adhoc_stdin",
]
