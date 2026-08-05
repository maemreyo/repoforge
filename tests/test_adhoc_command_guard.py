"""Pure git command-content guard for the reviewed ad-hoc runner escape hatch."""

from __future__ import annotations

import pytest

from repoforge.domain.adhoc import (
    CommandClass,
    EffectClass,
    classify_adhoc_command,
    classify_adhoc_effect,
    effect_exceeds_declaration,
    effect_to_command_class,
)
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


# ---------------------------------------------------------------------------
# #382: full EffectClass classification -- a superset of CommandClass, not a
# replacement. Every case here must agree with classify_adhoc_command's existing
# (unchanged) binary answer via effect_to_command_class.
# ---------------------------------------------------------------------------


def _assert_agrees_with_command_class(argv: tuple[str, ...]) -> EffectClass | None:
    """classify_adhoc_effect must never disagree with the older, still-relied-on
    classify_adhoc_command for the same argv -- they are two views of the same
    underlying classification, not two independent ones."""
    effect = classify_adhoc_effect(argv)
    command_class = classify_adhoc_command(argv)
    if effect is None:
        assert command_class is None
    else:
        assert command_class is effect_to_command_class(effect)
    return effect


@pytest.mark.parametrize(
    "argv",
    [
        ("git", "status", "--porcelain=v2"),
        ("git", "log", "--oneline", "-5"),
        ("git", "diff", "--cached"),
        ("git", "show", "HEAD"),
        ("git", "rev-parse", "HEAD"),
        ("git", "grep", "TODO"),
        ("git", "branch", "-a"),  # bare listing form, no mutating token
        ("git", "config", "--get", "remote.origin.url"),
    ],
)
def test_pure_local_reads_are_read_only_effect(argv: tuple[str, ...]) -> None:
    assert _assert_agrees_with_command_class(argv) is EffectClass.READ_ONLY


@pytest.mark.parametrize(
    "argv",
    [
        ("git", "fetch", "origin"),
        ("git", "ls-remote", "origin"),
        ("git", "-C", "packages/app", "-c", "core.pager=cat", "fetch"),
    ],
)
def test_network_reads_are_credentialed_network_effect_but_still_read_only_class(
    argv: tuple[str, ...],
) -> None:
    """#382 AC1: fetch/ls-remote must not be conflated with a pure local read, even
    though both stay CommandClass.READ_ONLY for exact-state-lock purposes (#445-era
    behavior, unchanged: fetch never moves HEAD or the workspace fingerprint)."""
    effect = _assert_agrees_with_command_class(argv)
    assert effect is EffectClass.CREDENTIALED_NETWORK
    assert classify_adhoc_command(argv) is CommandClass.READ_ONLY


@pytest.mark.parametrize(
    "argv",
    [
        ("git", "push", "origin", "main"),
        ("git", "push", "--force-with-lease=refs/heads/ai/x:" + _SHA, "origin", "ai/x"),
    ],
)
def test_allowed_push_forms_are_remote_write_effect(argv: tuple[str, ...]) -> None:
    assert _assert_agrees_with_command_class(argv) is EffectClass.REMOTE_WRITE


@pytest.mark.parametrize(
    "argv",
    [
        ("git", "commit", "-m", "wip"),
        ("git", "merge", "origin/main"),
        ("git", "rebase", "origin/main"),
        ("git", "cherry-pick", _SHA),
        ("git", "revert", _SHA),
        ("git", "checkout", "-b", "feature"),
        ("git", "switch", "-c", "feature"),
        ("git", "stash"),
        ("git", "reset", "--hard", "HEAD~1"),
        ("git", "branch", "-d", "old"),  # mutating token present
        ("git", "config", "--set", "user.name", "x"),
        ("git", "remote", "add", "upstream", "https://example.invalid/repo.git"),
        ("git", "tag", "v1.0.0"),
        ("git", "apply", "patch.diff"),
    ],
)
def test_local_mutations_are_local_history_effect(argv: tuple[str, ...]) -> None:
    assert _assert_agrees_with_command_class(argv) is EffectClass.LOCAL_HISTORY


def test_effect_risk_ordering_is_least_to_most_consequential() -> None:
    ordered = [
        EffectClass.READ_ONLY,
        EffectClass.WORKSPACE,
        EffectClass.LOCAL_HISTORY,
        EffectClass.CREDENTIALED_NETWORK,
        EffectClass.REMOTE_WRITE,
        EffectClass.DESTRUCTIVE_REMOTE,
    ]
    for i, lower in enumerate(ordered):
        for higher in ordered[i + 1 :]:
            assert effect_exceeds_declaration(higher, lower) is True
            assert effect_exceeds_declaration(lower, higher) is False
    for same in ordered:
        assert effect_exceeds_declaration(same, same) is False


def test_effect_mismatch_flags_network_reach_under_a_read_only_declaration() -> None:
    """#382 AC2: a command declared read_only that actually reached the network with
    credentials must produce mismatch evidence, not silently pass as matched."""
    fetch_effect = classify_adhoc_effect(("git", "fetch", "origin"))
    assert fetch_effect is not None
    assert effect_exceeds_declaration(fetch_effect, EffectClass.READ_ONLY) is True


def test_effect_mismatch_flags_remote_write_under_a_workspace_declaration() -> None:
    push_effect = classify_adhoc_effect(("git", "push", "origin", "main"))
    assert push_effect is not None
    assert effect_exceeds_declaration(push_effect, EffectClass.WORKSPACE) is True


def test_declaring_a_broader_effect_does_not_flag_a_narrower_observed_one() -> None:
    """The mismatch direction is one-way: over-declaring (stating more risk than a
    command turns out to have) is not itself a mismatch -- only under-declaring is."""
    status_effect = classify_adhoc_effect(("git", "status"))
    assert status_effect is EffectClass.READ_ONLY
    assert effect_exceeds_declaration(status_effect, EffectClass.DESTRUCTIVE_REMOTE) is False


def test_blocked_forms_still_raise_via_classify_adhoc_effect() -> None:
    """#382 must not weaken the existing block list: classify_adhoc_effect shares the
    exact same _assert_git_command_allowed call classify_adhoc_command does."""
    with pytest.raises(RepoForgeError) as excinfo:
        classify_adhoc_effect(("git", "push", "--force", "origin", "main"))
    assert excinfo.value.code is ErrorCode.ADHOC_COMMAND_FORBIDDEN


def test_classify_adhoc_effect_has_no_declared_effect_parameter() -> None:
    """#382 AC4, structurally: classify_adhoc_effect classifies argv alone. It cannot
    read a caller's declared_effect because that value is never in scope here -- the
    real proof this can't self-upgrade admission is that this function has nothing to
    upgrade with. See test_workspace_exec.py for the end-to-end version: the same argv
    is admitted or blocked identically regardless of the declared_effect a call sends."""
    import inspect

    assert list(inspect.signature(classify_adhoc_effect).parameters) == ["argv"]
