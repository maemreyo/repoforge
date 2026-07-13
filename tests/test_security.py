from pathlib import Path

import pytest

from repoforge.config import RepositoryConfig
from repoforge.domain.errors import SecurityError
from repoforge.domain.policy import (
    assert_path_allowed,
    extract_patch_paths,
    resolve_workspace_path,
    slugify,
    validate_patch,
)


def repo_config(tmp_path: Path) -> RepositoryConfig:
    return RepositoryConfig(
        repo_id="demo",
        path=tmp_path,
        denied_paths=(".env", ".github/workflows/**", "**/*.pem"),
    )


def test_slugify() -> None:
    assert slugify("Implement TODO Item #5") == "implement-todo-item-5"


def test_denied_paths(tmp_path: Path) -> None:
    repo = repo_config(tmp_path)
    assert assert_path_allowed("src/main.py", repo) == "src/main.py"
    with pytest.raises(SecurityError):
        assert_path_allowed(".env", repo)
    with pytest.raises(SecurityError):
        assert_path_allowed(".github/workflows/ci.yml", repo)


def test_path_escape(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    repo = repo_config(tmp_path)
    with pytest.raises(SecurityError):
        resolve_workspace_path(root, "../outside", repo)


def test_patch_paths() -> None:
    patch = """diff --git a/src/a.py b/src/a.py
--- a/src/a.py
+++ b/src/a.py
@@ -1 +1 @@
-old
+new
"""
    assert extract_patch_paths(patch) == ("src/a.py",)


def test_denied_generated_change_is_rejected(tmp_path: Path) -> None:
    # Policy checks apply even to files created outside the MCP write tools.
    repo = repo_config(tmp_path)
    with pytest.raises(SecurityError):
        assert_path_allowed("private.pem", repo)


def test_symlink_patch_is_rejected(tmp_path: Path) -> None:
    repo = repo_config(tmp_path)
    patch = r"""diff --git a/link b/link
new file mode 120000
index 0000000..1de5659
--- /dev/null
+++ b/link
@@ -0,0 +1 @@
+../outside
\ No newline at end of file
"""
    with pytest.raises(SecurityError, match="symlinks/submodules"):
        validate_patch(patch, repo, max_chars=10_000)
