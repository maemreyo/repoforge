from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from repoforge.application.service import CodingService
from repoforge.config import load_config
from repoforge.testing import CleanupTracker


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, text=True, capture_output=True
    ).stdout.strip()


@pytest.mark.skipif(shutil.which("git") is None, reason="git is required")
def test_real_git_workspace_lifecycle_preserves_phase5_contract(tmp_path: Path) -> None:
    remote = tmp_path / "remote.git"
    source = tmp_path / "source"
    subprocess.run(
        ["git", "init", "--bare", str(remote)], cwd=tmp_path, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "clone", str(remote), str(source)],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    _git(source, "config", "user.name", "RepoForge Test")
    _git(source, "config", "user.email", "repoforge@example.test")
    source.joinpath("hello.txt").write_text("hello\n", encoding="utf-8")
    _git(source, "add", ".")
    _git(source, "commit", "-m", "initial")
    _git(source, "branch", "-M", "main")
    _git(source, "push", "-u", "origin", "main")

    resolved = tmp_path / "resolved.toml"
    resolved.write_text(
        f'''[server]
workspace_root = "{tmp_path / "workspaces"}"
state_root = "{tmp_path / "state"}"
path_prefixes = ["/usr/local/bin", "/usr/bin", "/bin"]
allowed_environment = ["HOME", "PATH", "LANG"]

[repositories.demo]
path = "{source}"
remote = "origin"
default_base = "main"
allowed_base_branches = ["main"]
branch_prefix = "ai/"
protected_branches = ["main"]
read_only = false
require_verification_before_commit = true
fetch_before_workspace = true
default_verification_profile = "full"
max_changed_files = 10
max_diff_lines = 100
max_total_changed_bytes = 100000
allowed_paths = []
denied_paths = [".git", ".git/**", ".env"]
pr_labels = []
pr_reviewers = []
no_maintainer_edit = false

[repositories.demo.profiles.full]
description = "Full verification"
verification = true
commands = [[{sys.executable!r}, "-c", "from pathlib import Path; assert Path('hello.txt').read_text().strip() == 'changed'"]]
''',
        encoding="utf-8",
    )
    service = CodingService(load_config(resolved))
    baseline = CleanupTracker.capture(
        repo_path=source,
        workspace_root=tmp_path / "workspaces",
        state_root=tmp_path / "state",
    )
    created = service.workspace_create("demo", "real lifecycle")
    workspace_id = str(created["workspace_id"])
    workspace_path = Path(str(created["path"]))

    before = service.workspace_read_file(workspace_id, "hello.txt")
    service.workspace_write_file(workspace_id, "hello.txt", "changed\n", str(before["sha256"]))
    assert service.workspace_run_profile(workspace_id)["satisfies_commit_gate"] is True
    committed = service.workspace_commit(workspace_id, "change hello")
    pushed = service.workspace_push(workspace_id)
    assert pushed["head_sha"] == committed["head_sha"]
    assert _git(source, "ls-remote", "--heads", "origin", str(created["branch"]))

    service.workspace_remove(workspace_id, delete_local_branch=True)
    assert not workspace_path.exists()
    CleanupTracker.assert_no_leaks(
        baseline,
        repo_path=source,
        workspace_root=tmp_path / "workspaces",
        state_root=tmp_path / "state",
    )
