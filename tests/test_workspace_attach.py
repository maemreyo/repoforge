"""Attaching to a checkout git already tracks, without creating anything (#372).

Everything here runs against real git, same rationale as test_workspace_adopt_branch.py:
whether a worktree is discovered, missing, or detached is a fact about git's own state that
a fake helper could not usefully fake.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import ForgeEnvironment, git

from repoforge.domain.errors import SecurityError, WorkspaceError
from repoforge.domain.workspace import WorkspaceKind


def _checkout_new_branch_in_primary(env: ForgeEnvironment, name: str) -> str:
    """Leave the operator's own (primary) checkout on a fresh branch -- the literal
    "already checked out in the primary repository" case #372's AC names."""
    git("checkout", "-q", "-b", name, cwd=env.source)
    (env.source / "wip.txt").write_text("operator wip\n", encoding="utf-8")
    git("add", "wip.txt", cwd=env.source)
    git(
        "-c",
        "user.email=s@x",
        "-c",
        "user.name=s",
        "commit",
        "-q",
        "-m",
        f"wip on {name}",
        cwd=env.source,
    )
    return git("rev-parse", "HEAD", cwd=env.source)


def test_attach_to_the_primary_checkout_creates_nothing(forge_env: ForgeEnvironment) -> None:
    head = _checkout_new_branch_in_primary(forge_env, "wip/primary-checkout")
    workspace_root = forge_env.root / "workspaces"
    before = set(workspace_root.rglob("*")) if workspace_root.is_dir() else set()

    created = forge_env.service.workspace_create(
        "demo", "continue what I have open", attach_branch="wip/primary-checkout"
    )

    assert created["attached"] is True
    assert created["adopted_branch"] is False
    assert created["branch"] == "wip/primary-checkout"
    assert created["path"] == str(forge_env.source)
    assert created["head_sha"] == head
    after = set(workspace_root.rglob("*")) if workspace_root.is_dir() else set()
    assert after == before, "attach must not materialize anything under workspace_root"
    assert (forge_env.source / "wip.txt").read_text(encoding="utf-8") == "operator wip\n"


def test_attach_warns_about_reduced_isolation(forge_env: ForgeEnvironment) -> None:
    _checkout_new_branch_in_primary(forge_env, "wip/attach-warn")

    created = forge_env.service.workspace_create(
        "demo", "warned attach", attach_branch="wip/attach-warn"
    )

    warnings = created["warnings"]
    assert len(warnings) == 1
    assert "ATTACHED_SHARED" in warnings[0]
    assert "wip/attach-warn" in warnings[0]


def test_reattaching_the_same_checkout_returns_the_same_workspace_id(
    forge_env: ForgeEnvironment,
) -> None:
    _checkout_new_branch_in_primary(forge_env, "wip/reattach")

    first = forge_env.service.workspace_create(
        "demo", "first phrasing of the task", attach_branch="wip/reattach"
    )
    second = forge_env.service.workspace_create(
        "demo", "a completely different task_slug this time", attach_branch="wip/reattach"
    )

    assert first["workspace_id"] == second["workspace_id"]
    record = forge_env.service.state.load(first["workspace_id"])
    assert record.kind is WorkspaceKind.ATTACHED_SHARED


def test_reattach_self_heals_when_the_worktree_moved(forge_env: ForgeEnvironment) -> None:
    """A linked (non-primary) worktree can be moved with `git worktree move`; reattaching
    must follow it rather than treating the path change as a conflict."""
    linked = forge_env.root / "operator-linked-checkout"
    git("worktree", "add", "-b", "wip/movable", str(linked), "main", cwd=forge_env.source)

    first = forge_env.service.workspace_create("demo", "attach it", attach_branch="wip/movable")
    assert first["path"] == str(linked)

    moved = forge_env.root / "operator-linked-checkout-moved"
    git("worktree", "move", str(linked), str(moved), cwd=forge_env.source)

    second = forge_env.service.workspace_create(
        "demo", "attach it again", attach_branch="wip/movable"
    )

    assert second["workspace_id"] == first["workspace_id"]
    assert second["path"] == str(moved)
    record = forge_env.service.state.load(first["workspace_id"])
    assert record.path == str(moved)


def test_attach_unknown_branch_fails_with_evidence(forge_env: ForgeEnvironment) -> None:
    with pytest.raises(WorkspaceError, match="ATTACH_BRANCH_NOT_FOUND"):
        forge_env.service.workspace_create(
            "demo", "attach nothing", attach_branch="does-not-exist-anywhere"
        )


def test_attach_a_protected_branch_is_refused(forge_env: ForgeEnvironment) -> None:
    """The primary checkout starts on `main`, which is protected."""
    with pytest.raises(SecurityError, match="Protected branch"):
        forge_env.service.workspace_create("demo", "attach main", attach_branch="main")


def test_attach_an_unsafe_branch_name_is_refused(forge_env: ForgeEnvironment) -> None:
    for unsafe in ("../escape", "-oops", "bad//name", "trailing/"):
        with pytest.raises(SecurityError, match="Unsafe branch name"):
            forge_env.service.workspace_create("demo", "attach unsafe", attach_branch=unsafe)


def test_attach_and_base_together_are_refused(forge_env: ForgeEnvironment) -> None:
    _checkout_new_branch_in_primary(forge_env, "wip/conflict")

    with pytest.raises(WorkspaceError, match="ADOPT_BASE_CONFLICT"):
        forge_env.service.workspace_create(
            "demo", "conflicting", base="main", attach_branch="wip/conflict"
        )


def test_attach_and_adopt_together_are_refused(forge_env: ForgeEnvironment) -> None:
    _checkout_new_branch_in_primary(forge_env, "wip/also-conflict")

    with pytest.raises(WorkspaceError, match="ADOPT_BASE_CONFLICT"):
        forge_env.service.workspace_create(
            "demo",
            "conflicting",
            adopt_branch="wip/also-conflict",
            attach_branch="wip/also-conflict",
        )


def test_attach_a_missing_worktree_fails_with_evidence(forge_env: ForgeEnvironment) -> None:
    linked = forge_env.root / "linked-then-deleted"
    git("worktree", "add", "-b", "wip/missing", str(linked), "main", cwd=forge_env.source)
    # Delete the directory out from under git without telling it -- the missing-worktree
    # case #372's scope names, not a clean `git worktree remove`.
    for item in sorted(linked.rglob("*"), reverse=True):
        item.unlink() if item.is_file() else item.rmdir()
    linked.rmdir()

    with pytest.raises(WorkspaceError, match="ATTACH_WORKTREE_MISSING"):
        forge_env.service.workspace_create("demo", "attach missing", attach_branch="wip/missing")


def test_attach_ignores_a_detached_worktree_with_the_same_name_hint(
    forge_env: ForgeEnvironment,
) -> None:
    """A detached-HEAD worktree has no branch, so it must never match a branch-name query."""
    detached = forge_env.root / "detached-checkout"
    head = git("rev-parse", "main", cwd=forge_env.source)
    git("worktree", "add", "--detach", str(detached), head, cwd=forge_env.source)

    with pytest.raises(WorkspaceError, match="ATTACH_BRANCH_NOT_FOUND") as excinfo:
        forge_env.service.workspace_create(
            "demo", "attach detached", attach_branch="wip/never-existed"
        )
    assert excinfo.value.details["detached_worktree_count"] == 1


def test_removing_an_attached_workspace_is_refused(forge_env: ForgeEnvironment) -> None:
    _checkout_new_branch_in_primary(forge_env, "wip/attach-remove")
    created = forge_env.service.workspace_create(
        "demo", "attach then try remove", attach_branch="wip/attach-remove"
    )

    with pytest.raises(WorkspaceError, match="ATTACHED_WORKSPACE_NOT_REMOVABLE"):
        forge_env.service.workspace_remove(created["workspace_id"])

    assert Path(created["path"]).is_dir()
    assert forge_env.service.state.load(created["workspace_id"]).kind is (
        WorkspaceKind.ATTACHED_SHARED
    )
