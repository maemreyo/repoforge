"""Branch lifecycle and protected-ref guardrails across workspace kinds (#375).

Confirms two things that were already true by construction and needed proving, not new
code -- the git-content guard (classify_adhoc_command) and the protected-branch check
(validate_adopted_branch) take no workspace-kind parameter at all, so there is no kind or
execution mode that could bypass either -- and fixes one real gap this issue's own AC1
pointed at: `validate_branch` was still called directly (unconditionally requiring the
ai/* prefix) in workspace_push, workspace_pr, and workspace_refresh, even though
context.py's own gateway already let an adopted/attached branch through. An adopted branch
could be created and read, but not pushed or shipped as a PR, without hitting a spurious
"Branch must start with 'ai/'" refusal.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import ForgeEnvironment, create_forge_environment, git
from test_workspace_refresh import _commit_workspace_hello, _push_upstream_file

from repoforge.config import RepositoryConfig
from repoforge.domain.errors import ErrorCode, RepoForgeError, SecurityError, WorkspaceError
from repoforge.domain.policy import validate_workspace_branch
from repoforge.domain.workspace import WorkspaceKind

_DEMO_REPO = RepositoryConfig(
    repo_id="demo",
    path=Path("/dev/null"),
    protected_branches=("main", "master"),
)


def _existing_branch(env: ForgeEnvironment, name: str) -> None:
    """A branch that exists (for adopt_branch) but is NOT currently checked out anywhere --
    adopting only needs the branch to exist, and leaving the primary checkout on `main`
    keeps it free for other tests/helpers that assume that starting state."""
    git("checkout", "-q", "-b", name, cwd=env.source)
    (env.source / "wip.txt").write_text("wip\n", encoding="utf-8")
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
    git("checkout", "-q", "main", cwd=env.source)


def _checked_out_branch(env: ForgeEnvironment, name: str) -> None:
    """A branch that IS currently checked out in the primary checkout -- what attach_branch
    requires, unlike _existing_branch above."""
    git("checkout", "-q", "-b", name, cwd=env.source)
    (env.source / "wip.txt").write_text("wip\n", encoding="utf-8")
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


def _make_shared(env: ForgeEnvironment, workspace_id: str) -> None:
    record = env.service.state.load(workspace_id)
    record.kind = WorkspaceKind.ATTACHED_SHARED
    env.service.state.save(record)


# --- AC1: attached/adopted branches usable without the ai/* prefix, everywhere ---


def test_validate_workspace_branch_dispatches_by_kind() -> None:
    validate_workspace_branch(WorkspaceKind.MANAGED_WORKTREE, "ai/task-1", _DEMO_REPO)
    with pytest.raises(SecurityError, match="must start with"):
        validate_workspace_branch(WorkspaceKind.MANAGED_WORKTREE, "wip/task-1", _DEMO_REPO)

    for kind in (WorkspaceKind.ADOPTED_WORKTREE, WorkspaceKind.ATTACHED_SHARED):
        validate_workspace_branch(kind, "wip/task-1", _DEMO_REPO)  # no prefix required
        with pytest.raises(SecurityError, match="Protected branch"):
            validate_workspace_branch(kind, "main", _DEMO_REPO)
        with pytest.raises(SecurityError, match="Unsafe branch name"):
            validate_workspace_branch(kind, "../escape", _DEMO_REPO)


def test_adopted_branch_can_be_pushed(forge_env: ForgeEnvironment) -> None:
    """Regression: workspace_push called validate_branch directly, unconditionally,
    bypassing the kind-aware dispatch context.py's own gateway already applied."""
    _existing_branch(forge_env, "wip/pushable")
    created = forge_env.service.workspace_create(
        "demo", "adopt and push", adopt_branch="wip/pushable"
    )
    workspace_id = created["workspace_id"]
    _commit_workspace_hello(forge_env, workspace_id, "changed by agent\n")

    pushed = forge_env.service.workspace_push(workspace_id)

    assert pushed["pushed"] is True
    assert git("rev-parse", "wip/pushable", cwd=forge_env.remote) == pushed["head_sha"]


def test_adopted_branch_draft_pr_reaches_the_correct_refusal(
    forge_env: ForgeEnvironment,
) -> None:
    """Regression: create_draft_pr.py called validate_branch directly too, which would
    have refused this with "must start with 'ai/'" before even reaching the (separate,
    correct, and unrelated to #375) refusal every adopted workspace hits here: base ==
    branch always for an adopted branch (#372), so ahead_of_base's {remote}/{base}..HEAD
    is always zero once pushed. Proving THAT is the refusal -- not the prefix -- proves
    the fix, the same way the refresh test below proves it for workspace_refresh."""
    _existing_branch(forge_env, "wip/shippable")
    created = forge_env.service.workspace_create(
        "demo", "adopt and ship", adopt_branch="wip/shippable"
    )
    workspace_id = created["workspace_id"]
    _commit_workspace_hello(forge_env, workspace_id, "changed by agent\n")
    forge_env.service.workspace_push(workspace_id)

    with pytest.raises(WorkspaceError, match="no commits ahead of the base branch"):
        forge_env.service.workspace_pr(
            workspace_id,
            action="create_draft",
            title="Adopted branch PR",
            body="Ships from an adopted, non-ai/* branch.",
            idempotency_key="adopted-pr-create-0001",
        )


def test_adopted_branch_refresh_reaches_the_correct_refusal(
    forge_env: ForgeEnvironment,
) -> None:
    """Regression: refresh.py's execute() called validate_branch directly too, which
    would have refused this with "must start with 'ai/'" before even reaching the
    (separate, correct, and unrelated to #375) refusal every adopted workspace hits here:
    base == branch always for an adopted branch (#372), and base branches cannot be
    refreshed. Proving THAT is the refusal -- not the prefix -- proves the fix."""
    _existing_branch(forge_env, "wip/refreshable")
    created = forge_env.service.workspace_create(
        "demo", "adopt and refresh", adopt_branch="wip/refreshable"
    )
    workspace_id = created["workspace_id"]
    _commit_workspace_hello(forge_env, workspace_id, "changed by agent\n")
    forge_env.service.workspace_push(workspace_id)
    _push_upstream_file(forge_env, "upstream.txt", "upstream\n", "add upstream file")
    before = forge_env.service.workspace_status(workspace_id)
    preview = forge_env.service.workspace_refresh_preview(
        workspace_id, str(before["head_sha"]), str(before["workspace_fingerprint"])
    )

    with pytest.raises(WorkspaceError, match="Protected or base branches cannot be refreshed"):
        forge_env.service.workspace_refresh(
            workspace_id,
            str(preview["preview_id"]),
            str(before["head_sha"]),
            str(before["workspace_fingerprint"]),
        )


# --- AC4: branch-mismatch behavior is mode-aware ---


def test_managed_workspace_still_refuses_a_branch_switch(forge_env: ForgeEnvironment) -> None:
    workspace_id = forge_env.service.workspace_create("demo", "managed mismatch")["workspace_id"]
    path = forge_env.service.workspace_status(workspace_id)["path"]
    git("checkout", "-q", "-b", "someone-elses-branch", cwd=Path(path))

    with pytest.raises(WorkspaceError) as excinfo:
        forge_env.service.workspace_status(workspace_id)
    assert excinfo.value.code is ErrorCode.WORKSPACE_BRANCH_MISMATCH


def test_attached_shared_workspace_self_heals_a_branch_switch(
    forge_env: ForgeEnvironment,
) -> None:
    """The operator's own editor switching branches on a shared checkout is exactly the
    concurrent change #374/#375 exist to tolerate, not refuse -- as long as the branch
    landed on is itself one the workspace kind is allowed to hold. Switching onto a
    protected branch is a different, still-refused case; see the test below."""
    _checked_out_branch(forge_env, "wip/attach-target")
    created = forge_env.service.workspace_create(
        "demo", "attach then switch", attach_branch="wip/attach-target"
    )
    workspace_id = created["workspace_id"]
    _make_shared(forge_env, workspace_id)
    path = Path(created["path"])
    git("checkout", "-q", "-b", "wip/attach-target-2", cwd=path)
    # workspace_status computes ahead-of-base against origin/{base} unconditionally
    # (status.py), so the new base branch needs a remote counterpart -- exactly what a
    # genuine operator checkout would already have for a branch worth switching to.
    git("push", "-q", "origin", "wip/attach-target-2", cwd=path)

    status = forge_env.service.workspace_status(workspace_id)

    assert status["branch"] == "wip/attach-target-2"
    record = forge_env.service.state.load(workspace_id)
    assert record.branch == "wip/attach-target-2"
    assert record.base == "wip/attach-target-2"


def test_attached_shared_workspace_switch_to_protected_branch_still_refuses(
    forge_env: ForgeEnvironment,
) -> None:
    """Self-heal reconciles the registry with reality, but it must not turn a protected
    branch into a workspace RepoForge will operate on: AC2's "no bypass path in any kind"
    guarantee has to hold even for a state only reachable through self-heal itself.

    The observed branch is validated BEFORE it is persisted (review finding F-003): a
    denied branch must never be written into the registry as though it were the
    workspace's authorized state, even transiently. The registry therefore keeps the last
    branch that was actually authorized, not the protected one the switch was refused
    on -- self-heal is honest about drift only up to the point it would record something
    it is about to refuse."""
    _checked_out_branch(forge_env, "wip/attach-protected")
    created = forge_env.service.workspace_create(
        "demo", "attach then switch to main", attach_branch="wip/attach-protected"
    )
    workspace_id = created["workspace_id"]
    _make_shared(forge_env, workspace_id)
    path = Path(created["path"])
    git("checkout", "-q", "main", cwd=path)

    with pytest.raises(SecurityError, match="Protected branch"):
        forge_env.service.workspace_status(workspace_id)

    record = forge_env.service.state.load(workspace_id)
    assert record.branch == "wip/attach-protected"
    assert record.base == "wip/attach-protected"


# --- AC2: protected branches have no bypass path in any kind or execution mode ---


def test_protected_branch_is_refused_for_every_workspace_kind(
    forge_env: ForgeEnvironment,
) -> None:
    with pytest.raises(SecurityError, match="Protected branch"):
        forge_env.service.workspace_create("demo", "adopt main", adopt_branch="main")
    with pytest.raises(SecurityError, match="Protected branch"):
        forge_env.service.workspace_create("demo", "attach main", attach_branch="main")


# --- AC3: remote force/delete stays blocked by default in every autonomy mode ---


def test_force_push_is_blocked_for_every_workspace_kind(tmp_path: Path) -> None:
    """classify_adhoc_command's force-push guard (domain/adhoc.py) takes no workspace-kind
    parameter at all, so this is confirmation, not new behavior: there is structurally no
    kind that could bypass it. Uses workspace_run_adhoc directly (the synchronous path;
    see test_workspace_adhoc.py's own force-push coverage) with a relaxed, git-allowlisted
    environment, since the plain forge_env fixture's strict execution_mode would refuse the
    command before the force-push check is ever reached."""
    env = create_forge_environment(tmp_path, execution_mode="relaxed", adhoc_runners=("git",))
    service = env.service
    kinds_and_branches = []

    managed = service.workspace_create("demo", "managed force")
    kinds_and_branches.append((managed["workspace_id"], WorkspaceKind.MANAGED_WORKTREE))

    _existing_branch(env, "wip/adopted-force")
    adopted = service.workspace_create("demo", "adopted force", adopt_branch="wip/adopted-force")
    kinds_and_branches.append((adopted["workspace_id"], WorkspaceKind.ADOPTED_WORKTREE))

    _checked_out_branch(env, "wip/attached-force")
    attached = service.workspace_create(
        "demo", "attached force", attach_branch="wip/attached-force"
    )
    _make_shared(env, attached["workspace_id"])
    kinds_and_branches.append((attached["workspace_id"], WorkspaceKind.ATTACHED_SHARED))

    for workspace_id, kind in kinds_and_branches:
        record = service.state.load(workspace_id)
        assert record.kind is kind
        with pytest.raises(RepoForgeError, match="not permitted") as excinfo:
            service.workspace_run_adhoc(
                workspace_id, ["git", "push", "--force", "origin", record.branch]
            )
        assert excinfo.value.code is ErrorCode.ADHOC_COMMAND_FORBIDDEN
