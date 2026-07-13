"""Tests for the WorkspacePatchApplier typed application use case."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

import pytest

from repoforge.config import RepositoryConfig, load_config
from repoforge.errors import CommandError, SecurityError, WorkspaceError
from repoforge.runner import CommandRunner
from repoforge.security import validate_patch
from repoforge.state import StateStore
from repoforge.workspace_apply_patch import (
    WorkspaceApplyPatchCommand,
    WorkspaceApplyPatchPorts,
    WorkspacePatchApplier,
    head_sha,
    workspace_fingerprint,
)
from repoforge.workspace_create import (
    WorkspaceCreateCommand,
    WorkspaceCreator,
    WorkspaceCreatorPorts,
)


class ForgeEnvironmentProtocol(Protocol):
    """Minimal protocol describing what this test module accesses from forge_env."""

    config_path: Path


def _workspace_applier(
    forge_env: ForgeEnvironmentProtocol,
) -> tuple[WorkspacePatchApplier, RepositoryConfig, Path, str]:
    """Create a real workspace and return (applier, repo, workspace_path, workspace_id)."""
    config = load_config(forge_env.config_path)
    runner = CommandRunner(config.server)
    state = StateStore(config.server.state_root)

    creator = WorkspaceCreator(
        WorkspaceCreatorPorts(
            runner=runner,
            state=state,
            workspace_root=config.server.workspace_root,
            verification_timeout_seconds=config.server.verification_timeout_seconds,
        )
    )
    repo = config.repositories["demo"]
    plan = creator.plan(repo, WorkspaceCreateCommand("demo", "apply-patch-test"))
    created = creator.execute(repo, plan)

    applier = WorkspacePatchApplier(
        WorkspaceApplyPatchPorts(
            state=state,
            runner=runner,
            max_fingerprint_bytes=config.server.max_fingerprint_bytes,
            verification_timeout_seconds=config.server.verification_timeout_seconds,
        )
    )
    return applier, repo, created.path, created.workspace_id


def _load_server_config(forge_env: ForgeEnvironmentProtocol):
    """Load and return server config for test helpers."""
    return load_config(forge_env.config_path).server


# ---------------------------------------------------------------------------
# Happy path — apply a valid unified diff
# ---------------------------------------------------------------------------


def test_apply_patch_happy_path(forge_env: ForgeEnvironmentProtocol) -> None:
    applier, repo, workspace_path, ws_id = _workspace_applier(forge_env)
    cfg = _load_server_config(forge_env)
    runner = CommandRunner(cfg)

    head = head_sha(runner, workspace_path)
    fingerprint = workspace_fingerprint(
        runner,
        workspace_path,
        max_fingerprint_bytes=cfg.max_fingerprint_bytes,
        verification_timeout_seconds=cfg.verification_timeout_seconds,
    )

    patch = """diff --git a/README.md b/README.md
--- a/README.md
+++ b/README.md
@@ -1,3 +1,4 @@
 # Demo
 
 Repository instructions.
+Patched content.
"""
    changed_paths = validate_patch(patch, repo, max_chars=10000)

    result = applier.execute(
        repo,
        workspace_path,
        WorkspaceApplyPatchCommand(
            workspace_id=ws_id,
            patch=patch,
            expected_head_sha=head,
            expected_workspace_fingerprint=fingerprint,
        ),
        changed_paths,
    )

    assert result.workspace_id == ws_id
    assert "README.md" in result.changed_paths
    assert len(result.workspace_fingerprint) == 64
    assert isinstance(result.diff_stat, str)
    content = workspace_path.joinpath("README.md").read_text()
    assert "Patched content." in content


# ---------------------------------------------------------------------------
# Stale HEAD — HEAD SHA changed between status and apply
# ---------------------------------------------------------------------------


def test_apply_patch_stale_head(forge_env: ForgeEnvironmentProtocol) -> None:
    applier, repo, workspace_path, ws_id = _workspace_applier(forge_env)
    cfg = _load_server_config(forge_env)
    runner = CommandRunner(cfg)

    fingerprint = workspace_fingerprint(
        runner,
        workspace_path,
        max_fingerprint_bytes=cfg.max_fingerprint_bytes,
        verification_timeout_seconds=cfg.verification_timeout_seconds,
    )

    patch = """diff --git a/README.md b/README.md
--- a/README.md
+++ b/README.md
@@ -1,3 +1,4 @@
 # Demo
 
 Repository instructions.
+Stale.
"""
    changed_paths = validate_patch(patch, repo, max_chars=10000)

    with pytest.raises(WorkspaceError, match="HEAD changed"):
        _ = applier.execute(
            repo,
            workspace_path,
            WorkspaceApplyPatchCommand(
                workspace_id=ws_id,
                patch=patch,
                expected_head_sha="0000000000000000000000000000000000000000",
                expected_workspace_fingerprint=fingerprint,
            ),
            changed_paths,
        )


# ---------------------------------------------------------------------------
# Stale fingerprint — workspace changed between status and apply
# ---------------------------------------------------------------------------


def test_apply_patch_stale_fingerprint(forge_env: ForgeEnvironmentProtocol) -> None:
    applier, repo, workspace_path, ws_id = _workspace_applier(forge_env)
    cfg = _load_server_config(forge_env)
    runner = CommandRunner(cfg)

    head = head_sha(runner, workspace_path)

    # Compute fingerprint, then change the workspace
    fingerprint = workspace_fingerprint(
        runner,
        workspace_path,
        max_fingerprint_bytes=cfg.max_fingerprint_bytes,
        verification_timeout_seconds=cfg.verification_timeout_seconds,
    )

    # Modify a file to invalidate the fingerprint
    _ = (workspace_path / "hello.txt").write_text("modified\n")

    patch = """diff --git a/README.md b/README.md
--- a/README.md
+++ b/README.md
@@ -1,3 +1,4 @@
 # Demo
 
 Repository instructions.
+Stale fingerprint.
"""
    changed_paths = validate_patch(patch, repo, max_chars=10000)

    with pytest.raises(WorkspaceError, match="Workspace changed since"):
        _ = applier.execute(
            repo,
            workspace_path,
            WorkspaceApplyPatchCommand(
                workspace_id=ws_id,
                patch=patch,
                expected_head_sha=head,
                expected_workspace_fingerprint=fingerprint,
            ),
            changed_paths,
        )


# ---------------------------------------------------------------------------
# Direct format validation — malformed HEAD SHA via execute()
# ---------------------------------------------------------------------------


def test_apply_patch_rejects_invalid_head_sha_format(
    forge_env: ForgeEnvironmentProtocol,
) -> None:
    """Direct execute() must raise ValueError with the exact facade message for bad HEAD OID."""
    applier, repo, workspace_path, ws_id = _workspace_applier(forge_env)

    patch = """diff --git a/README.md b/README.md
--- a/README.md
+++ b/README.md
@@ -1,3 +1,4 @@
 # Demo
 
 Repository instructions.
+Bad OID.
"""
    # Workspace is clean; head_sha is valid, fingerprint is valid.
    cfg = _load_server_config(forge_env)
    runner = CommandRunner(cfg)
    head = head_sha(runner, workspace_path)
    fingerprint = workspace_fingerprint(
        runner,
        workspace_path,
        max_fingerprint_bytes=cfg.max_fingerprint_bytes,
        verification_timeout_seconds=cfg.verification_timeout_seconds,
    )

    with pytest.raises(ValueError, match="expected_head_sha must be a lowercase 40/64 hex Git object id"):
        _ = applier.execute(
            repo,
            workspace_path,
            WorkspaceApplyPatchCommand(
                workspace_id=ws_id,
                patch=patch,
                expected_head_sha="not-even-close",
                expected_workspace_fingerprint=fingerprint,
            ),
            ("README.md",),
        )

    # A 64-character SHA with uppercase hex is also invalid
    with pytest.raises(ValueError, match="expected_head_sha must be a lowercase 40/64 hex Git object id"):
        _ = applier.execute(
            repo,
            workspace_path,
            WorkspaceApplyPatchCommand(
                workspace_id=ws_id,
                patch=patch,
                expected_head_sha="F" * 40,
                expected_workspace_fingerprint=fingerprint,
            ),
            ("README.md",),
        )

    # A valid 40-char SHA passes format check (other checks may still fail)
    _ = applier.execute(
        repo,
        workspace_path,
        WorkspaceApplyPatchCommand(
            workspace_id=ws_id,
            patch=patch,
            expected_head_sha=head,
            expected_workspace_fingerprint=fingerprint,
        ),
        ("README.md",),
    )


# ---------------------------------------------------------------------------
# Direct format validation — malformed fingerprint SHA via execute()
# ---------------------------------------------------------------------------


def test_apply_patch_rejects_invalid_fingerprint_format(
    forge_env: ForgeEnvironmentProtocol,
) -> None:
    """Direct execute() must raise ValueError with the exact facade message for bad fingerprint."""
    applier, repo, workspace_path, ws_id = _workspace_applier(forge_env)

    cfg = _load_server_config(forge_env)
    runner = CommandRunner(cfg)
    head = head_sha(runner, workspace_path)

    patch = """diff --git a/README.md b/README.md
--- a/README.md
+++ b/README.md
@@ -1,3 +1,4 @@
 # Demo
 
 Repository instructions.
+Bad fingerprint.
"""

    with pytest.raises(ValueError, match="expected_workspace_fingerprint must be a lowercase SHA-256"):
        _ = applier.execute(
            repo,
            workspace_path,
            WorkspaceApplyPatchCommand(
                workspace_id=ws_id,
                patch=patch,
                expected_head_sha=head,
                expected_workspace_fingerprint="not-a-sha",
            ),
            ("README.md",),
        )


# ---------------------------------------------------------------------------
# git apply --check rejection — patch is syntactically valid but inapplicable
# ---------------------------------------------------------------------------


def test_apply_patch_invalid_patch_check_failure(
    forge_env: ForgeEnvironmentProtocol,
) -> None:
    """A patch that passes validate_patch but fails git apply --check raises CommandError."""
    applier, repo, workspace_path, ws_id = _workspace_applier(forge_env)
    cfg = _load_server_config(forge_env)
    runner = CommandRunner(cfg)

    head = head_sha(runner, workspace_path)
    fingerprint = workspace_fingerprint(
        runner,
        workspace_path,
        max_fingerprint_bytes=cfg.max_fingerprint_bytes,
        verification_timeout_seconds=cfg.verification_timeout_seconds,
    )

    # This patch has valid structure and paths, but the context line "wrong context"
    # does not match the actual file content ("hello").
    patch = """diff --git a/hello.txt b/hello.txt
--- a/hello.txt
+++ b/hello.txt
@@ -1 +1 @@
-wrong context
+new content
"""
    changed_paths = validate_patch(patch, repo, max_chars=10000)

    with pytest.raises(CommandError):
        _ = applier.execute(
            repo,
            workspace_path,
            WorkspaceApplyPatchCommand(
                workspace_id=ws_id,
                patch=patch,
                expected_head_sha=head,
                expected_workspace_fingerprint=fingerprint,
            ),
            changed_paths,
        )

    # The workspace must be unmodified
    assert workspace_path.joinpath("hello.txt").read_text() == "hello\n"


# ---------------------------------------------------------------------------
# Denied path — patch touches a path blocked by repository policy.
# validate_patch raises before the use case ever sees the patch.
# ---------------------------------------------------------------------------


def test_apply_patch_denied_path(forge_env: ForgeEnvironmentProtocol) -> None:
    _, repo, _workspace_path, _ws_id = _workspace_applier(forge_env)

    patch = """diff --git a/.env b/.env
new file mode 100644
--- /dev/null
+++ b/.env
@@ -0,0 +1 @@
+SECRET=leaked
"""
    with pytest.raises(SecurityError, match="denied"):
        _ = validate_patch(patch, repo, max_chars=10000)


# ---------------------------------------------------------------------------
# Post-apply policy violation triggers reverse-patch rollback
# ---------------------------------------------------------------------------


def test_apply_patch_post_apply_rollback(forge_env: ForgeEnvironmentProtocol) -> None:
    """An untracked symlink triggers the post-apply policy check, causing rollback."""
    applier, repo, workspace_path, ws_id = _workspace_applier(forge_env)
    cfg = _load_server_config(forge_env)
    runner = CommandRunner(cfg)

    # Create an untracked symlink in the workspace to trigger post-apply check
    real_target = workspace_path / "realfile.txt"
    _ = real_target.write_text("real content\n")
    (workspace_path / "link.txt").symlink_to("realfile.txt")

    # Capture head and fingerprint WITH the symlink present
    head = head_sha(runner, workspace_path)
    fingerprint = workspace_fingerprint(
        runner,
        workspace_path,
        max_fingerprint_bytes=cfg.max_fingerprint_bytes,
        verification_timeout_seconds=cfg.verification_timeout_seconds,
    )

    original_content = workspace_path.joinpath("hello.txt").read_text()

    patch = """diff --git a/hello.txt b/hello.txt
--- a/hello.txt
+++ b/hello.txt
@@ -1 +1 @@
-hello
+patched
"""
    changed_paths = validate_patch(patch, repo, max_chars=10000)

    with pytest.raises(SecurityError, match="symlink"):
        _ = applier.execute(
            repo,
            workspace_path,
            WorkspaceApplyPatchCommand(
                workspace_id=ws_id,
                patch=patch,
                expected_head_sha=head,
                expected_workspace_fingerprint=fingerprint,
            ),
            changed_paths,
        )

    # The patch must be rolled back — hello.txt is back to original
    assert workspace_path.joinpath("hello.txt").read_text() == original_content
    # The untracked symlink is still present (rollback only reverses the patch)
    assert workspace_path.joinpath("link.txt").is_symlink()
