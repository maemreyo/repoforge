"""F-012: release builds must come from an immutable commit snapshot.

A release directory is named by a commit SHA, so the wheel inside it must be built
from exactly that commit's tree. The old flow ran ``uv build`` in the caller's
working directory after a clean check, leaving a TOCTOU: a concurrent edit between
the check and the build ships a wheel carrying the commit's SHA but not the commit's
bytes. These tests pin the snapshot materialization helpers against a real
repository.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

import pytest

from repoforge.adapters.activation.build import (
    _commit_tree_sha,
    _materialize_snapshot,
    _persist_wheel,
)
from repoforge.domain.errors import ConfigError


def _git(*args: str, cwd: Path) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True, timeout=30
    )
    return completed.stdout.strip()


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git("init", "--quiet", cwd=root)
    _git("config", "user.email", "test@example.com", cwd=root)
    _git("config", "user.name", "Test User", cwd=root)
    return root


def _commit(root: Path, name: str, content: str) -> str:
    (root / name).write_text(content, encoding="utf-8")
    _git("add", name, cwd=root)
    _git("commit", "--quiet", "-m", f"add {name}", cwd=root)
    return _git("rev-parse", "HEAD", cwd=root)


def test_snapshot_contains_the_committed_tree_not_worktree_mutations(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _commit(root, "file.txt", "committed\n")
    # A concurrent edit after the clean check: the snapshot must still carry the
    # committed bytes, never the working-directory mutation.
    (root / "file.txt").write_text("mutated after clean check\n", encoding="utf-8")
    sha = _git("rev-parse", "HEAD", cwd=root)

    snapshot = _materialize_snapshot(root, sha)

    try:
        assert (snapshot / "file.txt").read_text(encoding="utf-8") == "committed\n"
    finally:
        import shutil

        shutil.rmtree(snapshot, ignore_errors=True)


def test_snapshot_is_pinned_to_the_resolved_sha_even_if_head_moved(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    first_sha = _commit(root, "file.txt", "first\n")
    _commit(root, "file.txt", "second\n")  # HEAD moves on, but the release is the first sha

    snapshot = _materialize_snapshot(root, first_sha)

    try:
        # The build input is the first commit's tree, not the worktree's new HEAD.
        assert (snapshot / "file.txt").read_text(encoding="utf-8") == "first\n"
    finally:
        import shutil

        shutil.rmtree(snapshot, ignore_errors=True)


def test_commit_tree_sha_is_the_immutable_content_digest(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    sha = _commit(root, "file.txt", "content\n")

    assert _commit_tree_sha(root, sha) == _git("rev-parse", f"{sha}^{{tree}}", cwd=root)

    # A working-directory edit does not change the committed tree digest.
    (root / "file.txt").write_text("uncommitted\n", encoding="utf-8")
    assert _commit_tree_sha(root, sha) == _git("rev-parse", f"{sha}^{{tree}}", cwd=root)


def test_materialize_snapshot_rejects_an_unknown_commit_and_cleans_up(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    before = set(Path(tempfile.gettempdir()).glob("repoforge-upgrade-snapshot-*"))

    with pytest.raises(ConfigError, match="SNAPSHOT_FAILED"):
        _materialize_snapshot(root, "0" * 40)

    after = set(Path(tempfile.gettempdir()).glob("repoforge-upgrade-snapshot-*"))
    assert after == before


def test_persist_wheel_outlives_the_build_scratch_directory(tmp_path: Path) -> None:
    """A built wheel must survive the build() cleanup the caller installs it after.

    Regression for the live-activation failure where the builder returned a wheel
    inside a tempdir its own ``finally`` deleted, so ``uv pip install`` found
    "Distribution not found at: file:...".
    """
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    wheel = scratch / "repoforge-2.2.0-py3-none-any.whl"
    wheel.write_bytes(b"fake wheel bytes")

    kept = _persist_wheel(wheel)

    try:
        assert kept.exists()
        assert kept.read_bytes() == b"fake wheel bytes"
        assert kept.suffix == ".whl"
        # uv pip install parses the wheel filename; a random one is rejected.
        assert kept.name == wheel.name
        # The caller removes the scratch dir; the kept wheel must not live inside it.
        import shutil

        shutil.rmtree(scratch, ignore_errors=True)
        assert kept.exists()
    finally:
        kept.unlink(missing_ok=True)
