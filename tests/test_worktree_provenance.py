"""The inspector must read the branch from real git, including the detached-HEAD case.

A release list is only memorable if the branch actually gets recorded, and the only way to
know that is to run it against a real repository -- a fake inspector would happily return
whatever the test wanted. Detached HEAD is covered because it is a NORMAL state for a
release build (the live activation harness checks out a bare sha) and it must degrade to
"unknown" rather than fail the upgrade.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from repoforge.adapters.activation.build import GitWorktreeInspector


def _git(*args: str, cwd: Path) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True, timeout=30
    )
    return completed.stdout.strip()


def _repo(tmp_path: Path, *, branch: str = "main", subject: str = "initial commit") -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git("init", "--quiet", f"--initial-branch={branch}", cwd=root)
    _git("config", "user.email", "test@example.com", cwd=root)
    _git("config", "user.name", "Test User", cwd=root)
    (root / "file.txt").write_text("content\n", encoding="utf-8")
    _git("add", "file.txt", cwd=root)
    _git("commit", "--quiet", "-m", subject, cwd=root)
    return root


def test_the_inspector_records_the_checked_out_branch_and_subject(tmp_path: Path) -> None:
    root = _repo(tmp_path, branch="feat/activation", subject="ship the activation gate")

    state = GitWorktreeInspector().inspect(root)

    assert state.branch == "feat/activation"
    assert state.subject == "ship the activation gate"
    assert state.clean is True
    assert state.head_sha == _git("rev-parse", "HEAD", cwd=root)


def test_a_detached_head_reports_no_branch_instead_of_failing(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _git("checkout", "--quiet", "--detach", "HEAD", cwd=root)

    state = GitWorktreeInspector().inspect(root)

    assert state.branch == ""
    # Everything that identifies the release still works; only the label is unknown.
    assert state.head_sha == _git("rev-parse", "HEAD", cwd=root)
    assert state.clean is True
    assert state.subject == "initial commit"


def test_a_dirty_worktree_still_reports_its_branch(tmp_path: Path) -> None:
    """The upgrade refuses a dirty worktree, and the refusal is more useful with a branch."""
    root = _repo(tmp_path, branch="wip")
    (root / "file.txt").write_text("changed\n", encoding="utf-8")

    state = GitWorktreeInspector().inspect(root)

    assert state.branch == "wip"
    assert state.clean is False
