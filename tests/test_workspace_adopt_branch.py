"""Adopting an existing branch: "work on THIS branch" must actually work on it.

Everything here runs against real git through the real adapter, because the whole point is
what git ends up doing -- a fake worktree helper could report any branch it liked.

The design is deliberately permissive: the ``ai/*`` naming convention and the base
allowlist do not apply to a branch the operator named, and the reduced isolation is
reported as a WARNING rather than refused. Two things stay refusals, and both are tested:
committing onto a protected branch, and deleting an adopted branch on removal.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import ForgeEnvironment, git

from repoforge.domain.errors import SecurityError, WorkspaceError


def _existing_branch(env: ForgeEnvironment, name: str, *, content: str = "wip work\n") -> str:
    """Create a branch in the source repo with a commit only that branch has."""
    git("checkout", "-q", "-b", name, cwd=env.source)
    (env.source / "wip.txt").write_text(content, encoding="utf-8")
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
    head = git("rev-parse", "HEAD", cwd=env.source)
    # Leave the source repo back on main: git refuses to check out a branch into a second
    # worktree while it is checked out here, which is a constraint the adopter must respect.
    git("checkout", "-q", "main", cwd=env.source)
    return head


def test_adopting_a_branch_puts_the_workspace_on_that_branch(forge_env: ForgeEnvironment) -> None:
    head = _existing_branch(forge_env, "wip/my-feature")

    created = forge_env.service.workspace_create(
        "demo", "continue the feature", adopt_branch="wip/my-feature"
    )

    assert created["branch"] == "wip/my-feature"
    assert created["adopted_branch"] is True
    # The workspace starts at the branch's own head, so the agent sees the work in progress.
    assert created["head_sha"] == head
    workspace = Path(created["path"])
    assert (workspace / "wip.txt").read_text(encoding="utf-8") == "wip work\n"
    assert git("rev-parse", "--abbrev-ref", "HEAD", cwd=workspace) == "wip/my-feature"


def test_adoption_warns_instead_of_refusing(forge_env: ForgeEnvironment) -> None:
    """The operator asked for it, so it proceeds -- but the reduced isolation is stated."""
    _existing_branch(forge_env, "wip/warned")

    created = forge_env.service.workspace_create("demo", "warned task", adopt_branch="wip/warned")

    warnings = created["warnings"]
    assert len(warnings) == 1
    assert "ADOPTED_BRANCH" in warnings[0]
    assert "wip/warned" in warnings[0]
    # The warning must say what is actually lost, not just that something is unusual.
    assert "isolation" in warnings[0]


def test_an_adopted_branch_keeps_the_operators_naming(forge_env: ForgeEnvironment) -> None:
    """The ai/* prefix rule is a convention for agent-created branches, not a gate here."""
    _existing_branch(forge_env, "feature/no-ai-prefix")

    created = forge_env.service.workspace_create(
        "demo", "prefixless", adopt_branch="feature/no-ai-prefix"
    )

    assert created["branch"] == "feature/no-ai-prefix"
    assert not created["branch"].startswith("ai/")


def test_commits_land_on_the_adopted_branch(forge_env: ForgeEnvironment) -> None:
    """The reason this feature exists: the agent's work must end up on the named branch."""
    _existing_branch(forge_env, "wip/lands-here")
    created = forge_env.service.workspace_create("demo", "land it", adopt_branch="wip/lands-here")
    workspace = Path(created["path"])

    (workspace / "agent.txt").write_text("agent change\n", encoding="utf-8")
    git("add", "agent.txt", cwd=workspace)
    git(
        "-c",
        "user.email=a@x",
        "-c",
        "user.name=a",
        "commit",
        "-q",
        "-m",
        "agent commit",
        cwd=workspace,
    )

    # The source repo's branch ref advanced -- one branch, shared, exactly as warned.
    assert git("rev-parse", "wip/lands-here", cwd=forge_env.source) == git(
        "rev-parse", "HEAD", cwd=workspace
    )
    assert "agent commit" in git("log", "-1", "--pretty=%s", "wip/lands-here", cwd=forge_env.source)


def test_adopting_a_protected_branch_is_still_refused(forge_env: ForgeEnvironment) -> None:
    """The one outcome a warning cannot undo: commits straight onto main."""
    with pytest.raises(SecurityError, match="Protected branch"):
        forge_env.service.workspace_create("demo", "onto main", adopt_branch="main")


def test_an_unsafe_branch_name_is_still_refused(forge_env: ForgeEnvironment) -> None:
    """`..` and a leading dash are argument injection into git, not a naming style."""
    for unsafe in ("../escape", "-oops", "bad//name", "trailing/"):
        with pytest.raises(SecurityError, match="Unsafe branch name"):
            forge_env.service.workspace_create("demo", "unsafe", adopt_branch=unsafe)


def test_adopt_and_base_together_are_refused(forge_env: ForgeEnvironment) -> None:
    """There is nothing to branch from when the branch already exists."""
    _existing_branch(forge_env, "wip/conflict")

    with pytest.raises(WorkspaceError, match="ADOPT_BASE_CONFLICT"):
        forge_env.service.workspace_create(
            "demo", "conflicting", base="main", adopt_branch="wip/conflict"
        )


def test_removing_the_workspace_never_deletes_an_adopted_branch(
    forge_env: ForgeEnvironment,
) -> None:
    """Deleting it would destroy work this workspace did not create."""
    _existing_branch(forge_env, "wip/keep-me")
    created = forge_env.service.workspace_create("demo", "keep it", adopt_branch="wip/keep-me")

    with pytest.raises(WorkspaceError, match="ADOPTED_BRANCH_NOT_DELETABLE"):
        forge_env.service.workspace_remove(created["workspace_id"], delete_local_branch=True)

    # Refused means nothing happened: the branch AND the workspace are still there.
    assert git("rev-parse", "--verify", "wip/keep-me", cwd=forge_env.source)
    removed = forge_env.service.workspace_remove(created["workspace_id"])
    assert removed["removed"] is True
    assert removed["local_branch_deleted"] is False
    assert git("rev-parse", "--verify", "wip/keep-me", cwd=forge_env.source)


def test_a_generated_branch_is_unaffected_by_the_new_option(forge_env: ForgeEnvironment) -> None:
    """The default path must be byte-for-byte the old behaviour."""
    created = forge_env.service.workspace_create("demo", "ordinary task")

    assert created["branch"].startswith("ai/")
    assert created["adopted_branch"] is False
    assert created["warnings"] == []
    assert created["base"] == "main"
