"""Shared consistency mode for attached_shared workspaces (#374): exact-state
preconditions become observations, not refusals -- except each mutation's own
expected_sha256, which still fails closed on a genuine collision with its target file.

attached_shared has no creation path outside #372/#373's attach flows, so these tests
flip an ordinary workspace's kind directly (same technique #371's own tests use) to
exercise the consistency-mode gate without needing a live external checkout.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import ForgeEnvironment, git

from repoforge.application.workspace.mutate import (
    CreateMutation,
    ReplaceTextMutation,
    TextReplacement,
)
from repoforge.domain.errors import WorkspaceError
from repoforge.domain.workspace import ConsistencyMode, WorkspaceKind


def _make_shared(env: ForgeEnvironment, workspace_id: str) -> None:
    record = env.service.state.load(workspace_id)
    assert record.kind.consistency_mode is ConsistencyMode.EXACT
    record.kind = WorkspaceKind.ATTACHED_SHARED
    env.service.state.save(record)


def test_workspace_kind_consistency_mode_mapping() -> None:
    assert WorkspaceKind.MANAGED_WORKTREE.consistency_mode is ConsistencyMode.EXACT
    assert WorkspaceKind.ADOPTED_WORKTREE.consistency_mode is ConsistencyMode.EXACT
    assert WorkspaceKind.ATTACHED_SHARED.consistency_mode is ConsistencyMode.SHARED


def test_exact_mode_still_refuses_stale_fingerprint(forge_env: ForgeEnvironment) -> None:
    """Unchanged behavior for a governed workspace (#374 AC)."""
    workspace_id = forge_env.service.workspace_create("demo", "exact fingerprint")["workspace_id"]
    with pytest.raises(WorkspaceError, match="changed since it was inspected"):
        forge_env.service.workspace_mutate(
            workspace_id,
            [CreateMutation(path="new.txt", content="new\n")],
            expected_workspace_fingerprint="0" * 64,
        )


def test_exact_mode_refuses_stale_head_sha(forge_env: ForgeEnvironment) -> None:
    """The head_sha check used to live only in the MCP dispatcher and was unreachable by a
    direct service call; it is now enforced in the application layer for every caller."""
    service = forge_env.service
    workspace_id = service.workspace_create("demo", "exact head")["workspace_id"]
    status = service.workspace_status(workspace_id)

    with pytest.raises(WorkspaceError, match="STALE_WORKSPACE_HEAD"):
        service.workspace_mutate(
            workspace_id,
            [CreateMutation(path="new.txt", content="new\n")],
            expected_workspace_fingerprint=status["workspace_fingerprint"],
            expected_head_sha="a" * 40,
        )


def test_shared_mode_allows_unrelated_dirty_tree(forge_env: ForgeEnvironment) -> None:
    service = forge_env.service
    workspace_id = service.workspace_create("demo", "shared unrelated dirty")["workspace_id"]
    path = Path(service.workspace_status(workspace_id)["path"])
    _make_shared(forge_env, workspace_id)

    # Simulate the operator's own editor touching an unrelated file concurrently.
    (path / "unrelated.txt").write_text("operator wrote this\n", encoding="utf-8")

    result = service.workspace_mutate(
        workspace_id,
        [CreateMutation(path="new_from_agent.txt", content="agent wrote this\n")],
        expected_workspace_fingerprint="0" * 64,  # deliberately stale/wrong
        expected_head_sha="f" * 40,  # deliberately stale/wrong
    )

    assert result["changed"] is True
    assert "new_from_agent.txt" in result["changed_paths"]
    observation = result["concurrent_observation"]
    assert observation is not None
    assert "unrelated.txt" in observation["dirty_paths"] + observation["untracked_paths"]
    assert observation["expected_head_sha"] == "f" * 40
    assert observation["expected_workspace_fingerprint"] == "0" * 64


def test_shared_mode_reports_no_observation_when_nothing_drifted(
    forge_env: ForgeEnvironment,
) -> None:
    service = forge_env.service
    workspace_id = service.workspace_create("demo", "shared no drift")["workspace_id"]
    status = service.workspace_status(workspace_id)
    _make_shared(forge_env, workspace_id)

    result = service.workspace_mutate(
        workspace_id,
        [CreateMutation(path="fresh.txt", content="fresh\n")],
        expected_workspace_fingerprint=status["workspace_fingerprint"],
        expected_head_sha=status["head_sha"],
    )

    assert result["changed"] is True
    assert result["concurrent_observation"] is None


def test_shared_mode_still_fails_closed_on_a_genuine_per_file_collision(
    forge_env: ForgeEnvironment,
) -> None:
    """The whole-tree gate is relaxed; the per-operation guard is not."""
    service = forge_env.service
    workspace_id = service.workspace_create("demo", "shared real collision")["workspace_id"]
    path = Path(service.workspace_status(workspace_id)["path"])
    hello = service.workspace_read_file(workspace_id, "hello.txt")
    _make_shared(forge_env, workspace_id)

    # Someone else changes THE SAME file the agent is about to target.
    (path / "hello.txt").write_text("someone else changed this\n", encoding="utf-8")
    git(
        "-c",
        "user.email=o@x",
        "-c",
        "user.name=o",
        "commit",
        "--allow-empty",
        "-q",
        "-m",
        "unrelated empty commit",
        cwd=path,
    )

    with pytest.raises(WorkspaceError, match="expected_sha256 mismatch"):
        service.workspace_mutate(
            workspace_id,
            [
                ReplaceTextMutation(
                    path="hello.txt",
                    expected_sha256=hello["sha256"],  # stale: no longer matches disk
                    edits=(TextReplacement("hello", "changed hello"),),
                )
            ],
            expected_workspace_fingerprint="0" * 64,
            expected_head_sha="f" * 40,
        )
