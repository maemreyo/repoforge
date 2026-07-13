from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path
from typing import Any

import pytest

from repoforge.application.context import ApplicationContext
from repoforge.application.workspace.create import (
    WorkspaceCreateCommand,
    WorkspaceCreator,
)
from repoforge.config import AppConfig, RepositoryConfig, ServerConfig
from repoforge.testing import InMemoryLockManager, InMemoryOperationGate


class FixedClock:
    def now_iso(self) -> str:
        return "2026-07-13T00:00:00+00:00"


class FixedIds:
    def new_hex(self, length: int = 10) -> str:
        return "a" * length


class NullAudit:
    path = Path("/tmp/audit.jsonl")

    def record(self, action: str, *, success: bool, details: dict[str, Any]) -> None:
        pass


class NullFiles:
    def exists(self, p):
        return p.exists()

    def is_dir(self, p):
        return p.is_dir()

    def is_file(self, p):
        return p.is_file()

    def is_symlink(self, p):
        return p.is_symlink()

    def size(self, p):
        return p.stat().st_size

    def read_bytes(self, p):
        return p.read_bytes()

    def read_text(self, p):
        return p.read_text()

    def write_bytes_atomic(self, p, data, *, preserve_mode=True):
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)

    def unlink(self, p, *, missing_ok=False):
        p.unlink(missing_ok=missing_ok)

    def mkdir(self, p, *, parents=True, exist_ok=True):
        p.mkdir(parents=parents, exist_ok=exist_ok)


class FailingStore:
    def __init__(self):
        self.records = {}

    def save(self, record):
        raise OSError("injected save failure")

    def load(self, workspace_id):
        return self.records[workspace_id]

    def delete(self, workspace_id):
        self.records.pop(workspace_id, None)

    def list(self):
        return list(self.records.values())

    def lock(self, workspace_id):
        return nullcontext()


class NullCommand:
    def environment(self, extra=None):
        return {}

    def run(self, *args, **kwargs):
        raise AssertionError("unexpected command")

    def run_bytes(self, *args, **kwargs):
        raise AssertionError("unexpected command")


class FakeGit:
    executor = None

    def __init__(self):
        self.created = False
        self.compensated = False

    def create_worktree(self, repo, destination, branch, base):
        destination.mkdir(parents=True)
        (destination / ".git").write_text("gitdir")
        self.created = True
        return "a" * 40

    def remove_worktree(self, repo, path, branch, delete_branch):
        self.compensated = True
        return delete_branch


class NullGithub:
    pass


class NullExecutable:
    pass


def test_workspace_creation_compensates_when_registry_save_fails(
    tmp_path: Path,
) -> None:
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    (repo_path / ".git").mkdir()
    workspace_root = tmp_path / "workspaces"
    state_root = tmp_path / "state"
    repo = RepositoryConfig(repo_id="demo", path=repo_path, fetch_before_workspace=False)
    config = AppConfig(
        tmp_path / "config.toml",
        ServerConfig(workspace_root, state_root),
        {"demo": repo},
    )
    git = FakeGit()
    ctx = ApplicationContext(
        config,
        NullCommand(),
        git,
        NullGithub(),
        NullFiles(),
        FailingStore(),
        InMemoryLockManager(),
        InMemoryOperationGate(),
        NullAudit(),
        FixedClock(),
        FixedIds(),
        NullExecutable(),
    )
    with pytest.raises(OSError, match="injected"):
        WorkspaceCreator(ctx).execute(WorkspaceCreateCommand("demo", "task"))
    assert git.created is True and git.compensated is True
