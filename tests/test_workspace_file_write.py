"""Tests for the WorkspaceFileWriter typed application use case."""

from __future__ import annotations

import hashlib
import stat
from pathlib import Path
from typing import Protocol

import pytest

from repoforge.config import RepositoryConfig, load_config
from repoforge.errors import SecurityError, WorkspaceError
from repoforge.runner import CommandRunner
from repoforge.state import StateStore
from repoforge.workspace_create import (
    WorkspaceCreateCommand,
    WorkspaceCreator,
    WorkspaceCreatorPorts,
)
from repoforge.workspace_file_write import (
    WorkspaceFileWriteCommand,
    WorkspaceFileWritePorts,
    WorkspaceFileWriter,
)


class ForgeEnvironmentProtocol(Protocol):
    """Minimal protocol describing what this test module accesses from forge_env."""

    config_path: Path


def _workspace_writer(
    forge_env: ForgeEnvironmentProtocol,
) -> tuple[WorkspaceFileWriter, RepositoryConfig, Path, str]:
    """Create a real workspace and return (writer, repo, workspace_path, workspace_id)."""
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
    plan = creator.plan(repo, WorkspaceCreateCommand("demo", "file-write-test"))
    created = creator.execute(repo, plan)

    writer = WorkspaceFileWriter(
        WorkspaceFileWritePorts(
            state=state,
            runner=runner,
            max_file_bytes=config.server.max_file_bytes,
        )
    )
    return writer, repo, created.path, created.workspace_id


# ---------------------------------------------------------------------------
# Happy path — create new file
# ---------------------------------------------------------------------------


def test_workspace_file_writer_creates_new_file(forge_env: ForgeEnvironmentProtocol) -> None:
    writer, repo, workspace_path, ws_id = _workspace_writer(forge_env)

    result = writer.execute(
        repo,
        workspace_path,
        WorkspaceFileWriteCommand(
            workspace_id=ws_id,
            relative_path="generated.txt",
            content="new content\n",
            expected_sha256="<new>",
        ),
    )

    assert result.path == "generated.txt"
    assert result.size_bytes == len(b"new content\n")
    assert len(result.sha256) == 64
    assert workspace_path.joinpath("generated.txt").read_text() == "new content\n"
    # diff_stat may be empty for a new untracked file — that matches the existing service behavior.
    assert isinstance(result.diff_stat, str)


# ---------------------------------------------------------------------------
# Happy path — overwrite existing file with correct SHA-256
# ---------------------------------------------------------------------------


def test_workspace_file_writer_overwrites_existing(forge_env: ForgeEnvironmentProtocol) -> None:
    writer, repo, workspace_path, ws_id = _workspace_writer(forge_env)

    # Given: a file already exists
    _ = writer.execute(
        repo,
        workspace_path,
        WorkspaceFileWriteCommand(
            workspace_id=ws_id,
            relative_path="overwrite-me.txt",
            content="original\n",
            expected_sha256="<new>",
        ),
    )
    original_sha = hashlib.sha256(b"original\n").hexdigest()

    # When: overwriting with the correct SHA
    result = writer.execute(
        repo,
        workspace_path,
        WorkspaceFileWriteCommand(
            workspace_id=ws_id,
            relative_path="overwrite-me.txt",
            content="updated\n",
            expected_sha256=original_sha,
        ),
    )

    # Then: content is replaced
    assert workspace_path.joinpath("overwrite-me.txt").read_text() == "updated\n"
    assert result.sha256 == hashlib.sha256(b"updated\n").hexdigest()


# ---------------------------------------------------------------------------
# Optimistic locking — stale SHA
# ---------------------------------------------------------------------------


def test_workspace_file_writer_rejects_stale_sha(forge_env: ForgeEnvironmentProtocol) -> None:
    writer, repo, workspace_path, ws_id = _workspace_writer(forge_env)

    _ = writer.execute(
        repo,
        workspace_path,
        WorkspaceFileWriteCommand(
            workspace_id=ws_id,
            relative_path="locking.txt",
            content="version one\n",
            expected_sha256="<new>",
        ),
    )

    stale_sha = hashlib.sha256(b"never written\n").hexdigest()
    with pytest.raises(WorkspaceError, match="changed since"):
        _ = writer.execute(
            repo,
            workspace_path,
            WorkspaceFileWriteCommand(
                workspace_id=ws_id,
                relative_path="locking.txt",
                content="version two\n",
                expected_sha256=stale_sha,
            ),
        )


# ---------------------------------------------------------------------------
# Content validation — NUL bytes
# ---------------------------------------------------------------------------


def test_workspace_file_writer_rejects_nul_bytes(forge_env: ForgeEnvironmentProtocol) -> None:
    writer, repo, workspace_path, ws_id = _workspace_writer(forge_env)

    with pytest.raises(SecurityError, match="NUL"):
        _ = writer.execute(
            repo,
            workspace_path,
            WorkspaceFileWriteCommand(
                workspace_id=ws_id,
                relative_path="clean.txt",
                content="bad\x00stuff\n",
                expected_sha256="<new>",
            ),
        )


# ---------------------------------------------------------------------------
# Content validation — exceeds max_file_bytes
# ---------------------------------------------------------------------------


def test_workspace_file_writer_rejects_oversized_content(
    forge_env: ForgeEnvironmentProtocol,
) -> None:
    writer, repo, workspace_path, ws_id = _workspace_writer(forge_env)
    oversized = "x" * (writer.max_file_bytes + 1)

    with pytest.raises(SecurityError, match="max_file_bytes"):
        _ = writer.execute(
            repo,
            workspace_path,
            WorkspaceFileWriteCommand(
                workspace_id=ws_id,
                relative_path="big.txt",
                content=oversized,
                expected_sha256="<new>",
            ),
        )


# ---------------------------------------------------------------------------
# Input validation — invalid SHA-256 format
# ---------------------------------------------------------------------------


def test_workspace_file_writer_rejects_invalid_sha_format(
    forge_env: ForgeEnvironmentProtocol,
) -> None:
    writer, repo, workspace_path, ws_id = _workspace_writer(forge_env)

    with pytest.raises(ValueError, match="expected_sha256"):
        _ = writer.execute(
            repo,
            workspace_path,
            WorkspaceFileWriteCommand(
                workspace_id=ws_id,
                relative_path="valid.txt",
                content="ok\n",
                expected_sha256="not-a-sha",
            ),
        )


# ---------------------------------------------------------------------------
# Security — non-regular-file rejection (directory + symlink-to-directory)
# ---------------------------------------------------------------------------


def test_workspace_file_writer_rejects_non_regular_file(
    forge_env: ForgeEnvironmentProtocol,
) -> None:
    """Writing to a directory raises SecurityError."""
    writer, repo, workspace_path, ws_id = _workspace_writer(forge_env)

    (workspace_path / "subdir").mkdir()
    sha = hashlib.sha256(b"content\n").hexdigest()

    with pytest.raises(SecurityError, match="regular file"):
        _ = writer.execute(
            repo,
            workspace_path,
            WorkspaceFileWriteCommand(
                workspace_id=ws_id,
                relative_path="subdir",
                content="content\n",
                expected_sha256=sha,
            ),
        )


def test_workspace_file_writer_rejects_symlink_to_directory(
    forge_env: ForgeEnvironmentProtocol,
) -> None:
    """Writing to a symlink pointing to a directory is rejected (resolved target is not a file)."""
    writer, repo, workspace_path, ws_id = _workspace_writer(forge_env)

    real_dir = workspace_path / "realdir"
    real_dir.mkdir()
    (workspace_path / "link_to_dir").symlink_to("realdir")
    sha = hashlib.sha256(b"content\n").hexdigest()

    with pytest.raises(SecurityError, match="symlink"):
        _ = writer.execute(
            repo,
            workspace_path,
            WorkspaceFileWriteCommand(
                workspace_id=ws_id,
                relative_path="link_to_dir",
                content="content\n",
                expected_sha256=sha,
            ),
        )


def test_workspace_file_writer_rejects_symlink_to_file(
    forge_env: ForgeEnvironmentProtocol,
) -> None:
    """Writing through a file symlink is rejected — the path must not be a symlink."""
    writer, repo, workspace_path, ws_id = _workspace_writer(forge_env)

    target = workspace_path / "realfile.txt"
    _ = target.write_text("original\n")
    (workspace_path / "link.txt").symlink_to("realfile.txt")
    original_sha = hashlib.sha256(b"original\n").hexdigest()

    with pytest.raises(SecurityError, match="symlink"):
        _ = writer.execute(
            repo,
            workspace_path,
            WorkspaceFileWriteCommand(
                workspace_id=ws_id,
                relative_path="link.txt",
                content="updated\n",
                expected_sha256=original_sha,
            ),
        )

    # The target file must be unchanged
    assert target.read_text() == "original\n"


# ---------------------------------------------------------------------------
# File mode preservation on overwrite
# ---------------------------------------------------------------------------


def test_workspace_file_writer_preserves_file_mode(
    forge_env: ForgeEnvironmentProtocol,
) -> None:
    writer, repo, workspace_path, ws_id = _workspace_writer(forge_env)

    exe = workspace_path / "script.sh"
    _ = exe.write_text("#!/bin/sh\necho hi\n")
    exe.chmod(0o755)
    sha = hashlib.sha256(b"#!/bin/sh\necho hi\n").hexdigest()

    _ = writer.execute(
        repo,
        workspace_path,
        WorkspaceFileWriteCommand(
            workspace_id=ws_id,
            relative_path="script.sh",
            content="echo updated\n",
            expected_sha256=sha,
        ),
    )

    assert stat.S_IMODE(exe.stat().st_mode) == 0o755


# ---------------------------------------------------------------------------
# Error — writing non-existent file without '<new>'
# ---------------------------------------------------------------------------


def test_workspace_file_writer_rejects_missing_without_new(
    forge_env: ForgeEnvironmentProtocol,
) -> None:
    writer, repo, workspace_path, ws_id = _workspace_writer(forge_env)
    sha = hashlib.sha256(b"content\n").hexdigest()

    with pytest.raises(WorkspaceError, match="does not exist"):
        _ = writer.execute(
            repo,
            workspace_path,
            WorkspaceFileWriteCommand(
                workspace_id=ws_id,
                relative_path="absent.txt",
                content="content\n",
                expected_sha256=sha,
            ),
        )


# ---------------------------------------------------------------------------
# Error — writing existing file with '<new>'
# ---------------------------------------------------------------------------


def test_workspace_file_writer_rejects_existing_with_new(
    forge_env: ForgeEnvironmentProtocol,
) -> None:
    writer, repo, workspace_path, ws_id = _workspace_writer(forge_env)

    _ = writer.execute(
        repo,
        workspace_path,
        WorkspaceFileWriteCommand(
            workspace_id=ws_id,
            relative_path="present.txt",
            content="exists\n",
            expected_sha256="<new>",
        ),
    )

    with pytest.raises(WorkspaceError, match="already exists"):
        _ = writer.execute(
            repo,
            workspace_path,
            WorkspaceFileWriteCommand(
                workspace_id=ws_id,
                relative_path="present.txt",
                content="still here\n",
                expected_sha256="<new>",
            ),
        )


# ---------------------------------------------------------------------------
# Denied path — workspace policy blocks the path
# ---------------------------------------------------------------------------


def test_workspace_file_writer_rejects_denied_path(forge_env: ForgeEnvironmentProtocol) -> None:
    writer, repo, workspace_path, ws_id = _workspace_writer(forge_env)

    with pytest.raises(SecurityError, match="denied"):
        _ = writer.execute(
            repo,
            workspace_path,
            WorkspaceFileWriteCommand(
                workspace_id=ws_id,
                relative_path=".github/workflows/evil.yml",
                content="name: evil\n",
                expected_sha256="<new>",
            ),
        )
