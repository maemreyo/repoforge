"""Workspace kind is the typed discriminator for ownership, replacing the old
``metadata.get("adopted_branch")`` boolean flag (#371).

managed_worktree and adopted_worktree are exercised through the real service; attached_shared
has no creation path yet (#372/#373 own that), so its removal-safety guard is exercised by
constructing the record directly, the same way an attach flow will eventually produce one.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest
from conftest import ForgeEnvironment

from repoforge.adapters.persistence.json_workspace_store import JsonWorkspaceStore
from repoforge.domain.errors import WorkspaceError
from repoforge.domain.workspace import WorkspaceKind, WorkspaceRecord
from repoforge.testing import InMemoryLockManager


def test_workspace_update_serializes_concurrent_metadata_changes(tmp_path: Path) -> None:
    locks = InMemoryLockManager()
    first = JsonWorkspaceStore(tmp_path, locks)
    second = JsonWorkspaceStore(tmp_path, locks)
    first.save(
        WorkspaceRecord(
            "ws-1",
            "demo",
            "/tmp/ws-1",
            "ai/task-1",
            "main",
            "origin",
            "2026-01-01T00:00:00Z",
        )
    )
    start = threading.Barrier(2)

    def add_metadata(store: JsonWorkspaceStore, key: str) -> None:
        start.wait(timeout=2.0)
        store.update("ws-1", lambda record: record.metadata.__setitem__(key, True))

    threads = (
        threading.Thread(target=add_metadata, args=(first, "failure_evidence")),
        threading.Thread(target=add_metadata, args=(second, "pr_intent")),
    )
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2.0)

    assert all(not thread.is_alive() for thread in threads)
    assert first.load("ws-1").metadata == {
        "failure_evidence": True,
        "pr_intent": True,
    }
    raw = json.loads((first.registry_dir / "ws-1.json").read_text(encoding="utf-8"))
    assert raw["revision"] == 3


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


def test_attached_shared_workspace_removal_detaches_registry_only(
    forge_env: ForgeEnvironment,
) -> None:
    """Removing an attached-shared workspace must never touch its checkout, but it must
    still be able to forget RepoForge's own registry entry -- that bookkeeping is the only
    thing workspace_remove owns for this kind (review finding F-002)."""
    created = forge_env.service.workspace_create("demo", "future attach")
    workspace_id = created["workspace_id"]
    record = forge_env.service.state.load(workspace_id)
    record.kind = WorkspaceKind.ATTACHED_SHARED
    forge_env.service.state.save(record)

    result = forge_env.service.workspace_remove(workspace_id)

    assert result["removed"] is True
    assert result["local_branch_deleted"] is False
    with pytest.raises(WorkspaceError, match="Unknown workspace id"):
        forge_env.service.state.load(workspace_id)
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
