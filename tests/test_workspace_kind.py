"""Workspace kind is the typed discriminator for ownership, replacing the old
``metadata.get("adopted_branch")`` boolean flag (#371).

managed_worktree and adopted_worktree are exercised through the real service; attached_shared
has no creation path yet (#372/#373 own that), so its removal-safety guard is exercised by
constructing the record directly, the same way an attach flow will eventually produce one.
"""

from __future__ import annotations

import json

import pytest
from conftest import ForgeEnvironment

from repoforge.adapters.persistence.json_workspace_store import JsonWorkspaceStore
from repoforge.domain.errors import WorkspaceError
from repoforge.domain.workspace import WorkspaceKind, WorkspaceRecord


def test_ordinary_create_is_managed_worktree(forge_env: ForgeEnvironment) -> None:
    created = forge_env.service.workspace_create("demo", "ordinary task")
    record = forge_env.service.state.load(created["workspace_id"])

    assert record.kind is WorkspaceKind.MANAGED_WORKTREE
    assert record.kind.owns_branch_lifecycle is True
    assert record.kind.owns_worktree_lifecycle is True
    assert record.kind.naming_convention_enforced is True


def test_adopted_create_is_adopted_worktree(forge_env: ForgeEnvironment) -> None:
    from conftest import git

    git("checkout", "-q", "-b", "wip/kind-check", cwd=forge_env.source)
    git("checkout", "-q", "main", cwd=forge_env.source)

    created = forge_env.service.workspace_create("demo", "adopt it", adopt_branch="wip/kind-check")
    record = forge_env.service.state.load(created["workspace_id"])

    assert record.kind is WorkspaceKind.ADOPTED_WORKTREE
    assert record.kind.owns_branch_lifecycle is False
    assert record.kind.owns_worktree_lifecycle is True
    assert record.kind.naming_convention_enforced is False


def test_legacy_registry_record_without_kind_defaults_to_managed(
    forge_env: ForgeEnvironment,
) -> None:
    """A record written before #371 has no "kind" key at all; it must load as managed,
    not raise, and not silently compare unequal to every WorkspaceKind member."""
    created = forge_env.service.workspace_create("demo", "pre-existing record")
    workspace_id = created["workspace_id"]
    store = JsonWorkspaceStore(forge_env.root / "state")
    record_path = store.registry_dir / f"{workspace_id}.json"
    raw = json.loads(record_path.read_text(encoding="utf-8"))
    # Simulate a registry entry written before this field existed.
    del raw["kind"]
    record_path.write_text(json.dumps(raw), encoding="utf-8")

    loaded = store.load(workspace_id)

    assert loaded.kind is WorkspaceKind.MANAGED_WORKTREE


def test_corrupt_kind_on_disk_fails_closed(forge_env: ForgeEnvironment) -> None:
    created = forge_env.service.workspace_create("demo", "corrupt kind")
    workspace_id = created["workspace_id"]
    store = JsonWorkspaceStore(forge_env.root / "state")
    record_path = store.registry_dir / f"{workspace_id}.json"
    raw = json.loads(record_path.read_text(encoding="utf-8"))
    raw["kind"] = "not_a_real_kind"
    record_path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(WorkspaceError, match="invalid kind"):
        store.load(workspace_id)


def test_attached_shared_workspace_removal_is_refused(forge_env: ForgeEnvironment) -> None:
    """No attach flow exists yet (#372/#373), so this constructs the record directly --
    the same shape a future attach flow will produce -- to prove the removal guard already
    protects a kind whose creation path does not exist yet."""
    created = forge_env.service.workspace_create("demo", "future attach")
    workspace_id = created["workspace_id"]
    record = forge_env.service.state.load(workspace_id)
    record.kind = WorkspaceKind.ATTACHED_SHARED
    forge_env.service.state.save(record)

    with pytest.raises(WorkspaceError, match="ATTACHED_WORKSPACE_NOT_REMOVABLE"):
        forge_env.service.workspace_remove(workspace_id)

    # Refused means nothing happened: the checkout and the registry entry are untouched.
    assert forge_env.service.state.load(workspace_id).kind is WorkspaceKind.ATTACHED_SHARED
    from pathlib import Path

    assert Path(created["path"]).is_dir()


def test_workspace_record_rejects_invalid_kind_at_construction() -> None:
    with pytest.raises(WorkspaceError, match="invalid kind"):
        WorkspaceRecord(
            "ws-1",
            "demo",
            "/tmp/ws-1",
            "ai/task-1",
            "main",
            "origin",
            "2026-01-01T00:00:00Z",
            kind="not_a_real_kind",  # type: ignore[arg-type]
        )
