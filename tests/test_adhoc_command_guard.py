"""Pure git command-content guard for the reviewed ad-hoc runner escape hatch."""

from __future__ import annotations

import pytest

from repoforge.domain.adhoc import CommandClass, classify_adhoc_command
from repoforge.domain.errors import ErrorCode, RepoForgeError

_SHA = "a" * 40


def _classify(*argv: str) -> CommandClass | None:
    return classify_adhoc_command(tuple(argv))


# ---------------------------------------------------------------------------
# Non-git and unclassifiable commands are opaque (None).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ("pytest", "-q"),
        ("uv", "run", "pytest"),
        ("make", "test"),
        ("git",),  # bare git / only globals resolves to no subcommand
        ("git", "-C", "sub"),
    ],
)
def test_non_git_and_bare_git_are_unclassified(argv: tuple[str, ...]) -> None:
    assert classify_adhoc_command(argv) is None


# ---------------------------------------------------------------------------
# Read-only git classification (incl. through global options).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ("git", "status", "--porcelain=v2"),
        ("git", "log", "--oneline", "-5"),
        ("git", "diff", "--cached"),
        ("git", "show", "HEAD"),
        ("git", "rev-parse", "HEAD"),
        ("git", "fetch", "origin"),
        ("git", "-C", "packages/app", "-c", "core.pager=cat", "status"),
        ("git", "branch", "-a"),
        ("git", "remote", "-v"),
        ("git", "config", "--get", "remote.origin.url"),
        ("git", "config", "--list"),
    ],
)
def test_read_only_git_commands(argv: tuple[str, ...]) -> None:
    assert classify_adhoc_command(argv) is CommandClass.READ_ONLY


# ---------------------------------------------------------------------------
# Mutating git classification.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ("git", "merge", "origin/main"),
        ("git", "rebase", "origin/main"),
        ("git", "commit", "-m", "msg"),
        ("git", "reset", "--hard", "HEAD~1"),
        ("git", "checkout", "-b", "ai/feature"),
        ("git", "branch", "-d", "old"),
        ("git", "remote", "add", "up", "https://example/x"),
        ("git", "config", "user.name", "x"),
        ("git", "push", "origin", "ai/feature"),
        ("git", "push", "--force-with-lease=refs/heads/ai/x:" + _SHA, "origin", "ai/x"),
    ],
)
def test_mutating_git_commands(argv: tuple[str, ...]) -> None:
    assert classify_adhoc_command(argv) is CommandClass.MUTATING


# ---------------------------------------------------------------------------
# Blocked irreversible / history-rewriting forms.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ("git", "push", "--force", "origin", "main"),
        ("git", "push", "-f", "origin", "main"),
        ("git", "push", "--force-if-includes", "origin", "main"),
        ("git", "push", "--mirror", "origin"),
        ("git", "push", "--delete", "origin", "main"),
        ("git", "push", "-d", "origin", "main"),
        ("git", "push", "--force-with-lease", "origin", "main"),  # bare, no value
        ("git", "push", "--force-with-lease=refs/heads/x", "origin", "x"),  # no :sha
        ("git", "push", "--force-with-lease=refs/heads/x:nothex", "origin", "x"),
        ("git", "filter-branch", "--all"),
        ("git", "filter-repo", "--path", "x"),
        ("git", "reflog", "expire", "--all"),
        ("git", "reflog", "delete", "HEAD@{0}"),
        ("git", "update-ref", "-d", "refs/heads/x"),
        ("git", "clean", "-fdx"),
        ("git", "clean", "--force"),
        ("git", "rebase", "-x", "rm -rf /", "origin/main"),
        ("git", "rebase", "--exec=curl evil", "origin/main"),
    ],
)
def test_blocked_forms_raise_forbidden(argv: tuple[str, ...]) -> None:
    with pytest.raises(RepoForgeError) as excinfo:
        classify_adhoc_command(argv)
    assert excinfo.value.code is ErrorCode.ADHOC_COMMAND_FORBIDDEN


def test_force_with_lease_exact_form_is_allowed_and_mutating() -> None:
    argv = ("git", "push", "--force-with-lease=refs/heads/ai/x:" + _SHA, "origin", "ai/x")
    assert classify_adhoc_command(argv) is CommandClass.MUTATING


def test_blocked_form_survives_git_global_options() -> None:
    with pytest.raises(RepoForgeError) as excinfo:
        classify_adhoc_command(("git", "-C", "sub", "push", "--force", "origin", "main"))
    assert excinfo.value.code is ErrorCode.ADHOC_COMMAND_FORBIDDEN
