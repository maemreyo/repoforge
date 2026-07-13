"""Tests for the WorkspaceTextReplacer typed application use case."""

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
from repoforge.workspace_replace_text import (
    WorkspaceReplaceTextCommand,
    WorkspaceReplaceTextPorts,
    WorkspaceTextReplacer,
)


class ForgeEnvironmentProtocol(Protocol):
    """Minimal protocol describing what this test module accesses from forge_env."""

    config_path: Path


def _workspace_replacer(
    forge_env: ForgeEnvironmentProtocol,
    *,
    max_file_bytes: int | None = None,
) -> tuple[WorkspaceTextReplacer, RepositoryConfig, Path, str]:
    """Create a real workspace and return (replacer, repo, workspace_path, workspace_id)."""
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
    plan = creator.plan(repo, WorkspaceCreateCommand("demo", "replace-text-test"))
    created = creator.execute(repo, plan)

    replacer = WorkspaceTextReplacer(
        WorkspaceReplaceTextPorts(
            state=state,
            runner=runner,
            max_file_bytes=max_file_bytes or config.server.max_file_bytes,
        )
    )
    return replacer, repo, created.path, created.workspace_id


# ---------------------------------------------------------------------------
# Happy path — replace exact text
# ---------------------------------------------------------------------------


def test_replace_text_happy_path(forge_env: ForgeEnvironmentProtocol) -> None:
    replacer, repo, workspace_path, ws_id = _workspace_replacer(forge_env)

    original = "hello world\nthis is the old text\nmore content\n"
    _ = (workspace_path / "example.txt").write_text(original)
    sha = hashlib.sha256(original.encode("utf-8")).hexdigest()

    result = replacer.execute(
        repo,
        workspace_path,
        WorkspaceReplaceTextCommand(
            workspace_id=ws_id,
            relative_path="example.txt",
            old_text="old text",
            new_text="new text",
            expected_sha256=sha,
            expected_occurrences=1,
        ),
    )

    assert result.path == "example.txt"
    assert result.replacements == 1
    expected_content = "hello world\nthis is the new text\nmore content\n"
    assert workspace_path.joinpath("example.txt").read_text() == expected_content
    assert result.sha256 == hashlib.sha256(expected_content.encode("utf-8")).hexdigest()
    assert isinstance(result.diff_stat, str)


# ---------------------------------------------------------------------------
# Happy path — multiple occurrences
# ---------------------------------------------------------------------------


def test_replace_text_replaces_multiple_occurrences(
    forge_env: ForgeEnvironmentProtocol,
) -> None:
    replacer, repo, workspace_path, ws_id = _workspace_replacer(forge_env)

    original = "foo bar foo bar foo bar\n"
    _ = (workspace_path / "multi.txt").write_text(original)
    sha = hashlib.sha256(original.encode("utf-8")).hexdigest()

    result = replacer.execute(
        repo,
        workspace_path,
        WorkspaceReplaceTextCommand(
            workspace_id=ws_id,
            relative_path="multi.txt",
            old_text="foo",
            new_text="baz",
            expected_sha256=sha,
            expected_occurrences=3,
        ),
    )

    assert result.replacements == 3
    expected = "baz bar baz bar baz bar\n"
    assert workspace_path.joinpath("multi.txt").read_text() == expected


# ---------------------------------------------------------------------------
# Happy path — no modification when old_text == new_text
# ---------------------------------------------------------------------------


def test_replace_text_no_modification(forge_env: ForgeEnvironmentProtocol) -> None:
    """Replacing old_text with the same text is a no-op but succeeds."""
    replacer, repo, workspace_path, ws_id = _workspace_replacer(forge_env)

    original = "same content\n"
    _ = (workspace_path / "noop.txt").write_text(original)
    sha = hashlib.sha256(original.encode("utf-8")).hexdigest()

    result = replacer.execute(
        repo,
        workspace_path,
        WorkspaceReplaceTextCommand(
            workspace_id=ws_id,
            relative_path="noop.txt",
            old_text="content",
            new_text="content",
            expected_sha256=sha,
            expected_occurrences=1,
        ),
    )

    assert result.replacements == 1
    assert result.sha256 == sha
    assert workspace_path.joinpath("noop.txt").read_text() == original


# ---------------------------------------------------------------------------
# Optimistic locking — stale SHA
# ---------------------------------------------------------------------------


def test_replace_text_rejects_stale_sha(forge_env: ForgeEnvironmentProtocol) -> None:
    replacer, repo, workspace_path, ws_id = _workspace_replacer(forge_env)

    original = "stale content\n"
    _ = (workspace_path / "stale.txt").write_text(original)
    actual_sha = hashlib.sha256(original.encode("utf-8")).hexdigest()

    # Mutate the file after reading SHA
    _ = (workspace_path / "stale.txt").write_text("modified content\n")

    stale_sha = actual_sha
    with pytest.raises(WorkspaceError, match="changed since"):
        _ = replacer.execute(
            repo,
            workspace_path,
            WorkspaceReplaceTextCommand(
                workspace_id=ws_id,
                relative_path="stale.txt",
                old_text="modified",
                new_text="updated",
                expected_sha256=stale_sha,
                expected_occurrences=1,
            ),
        )


# ---------------------------------------------------------------------------
# Expected occurrence mismatch
# ---------------------------------------------------------------------------


def test_replace_text_rejects_wrong_occurrence_count(
    forge_env: ForgeEnvironmentProtocol,
) -> None:
    replacer, repo, workspace_path, ws_id = _workspace_replacer(forge_env)

    original = "only one occurrence\n"
    _ = (workspace_path / "occur.txt").write_text(original)
    sha = hashlib.sha256(original.encode("utf-8")).hexdigest()

    with pytest.raises(WorkspaceError, match="Expected 2 occurrences, found 1"):
        _ = replacer.execute(
            repo,
            workspace_path,
            WorkspaceReplaceTextCommand(
                workspace_id=ws_id,
                relative_path="occur.txt",
                old_text="occurrence",
                new_text="match",
                expected_sha256=sha,
                expected_occurrences=2,
            ),
        )

    # Original file must be unmodified
    assert workspace_path.joinpath("occur.txt").read_text() == original


# ---------------------------------------------------------------------------
# Security — denied path by repository policy
# ---------------------------------------------------------------------------


def test_replace_text_rejects_denied_path(forge_env: ForgeEnvironmentProtocol) -> None:
    replacer, repo, workspace_path, ws_id = _workspace_replacer(forge_env)

    with pytest.raises(SecurityError, match="denied"):
        _ = replacer.execute(
            repo,
            workspace_path,
            WorkspaceReplaceTextCommand(
                workspace_id=ws_id,
                relative_path=".github/workflows/evil.yml",
                old_text="x",
                new_text="y",
                expected_sha256="a" * 64,
                expected_occurrences=1,
            ),
        )


# ---------------------------------------------------------------------------
# Security — path traversal
# ---------------------------------------------------------------------------


def test_replace_text_rejects_traversal(forge_env: ForgeEnvironmentProtocol) -> None:
    replacer, repo, workspace_path, ws_id = _workspace_replacer(forge_env)

    with pytest.raises(SecurityError, match="normalized"):
        _ = replacer.execute(
            repo,
            workspace_path,
            WorkspaceReplaceTextCommand(
                workspace_id=ws_id,
                relative_path="../outside.txt",
                old_text="x",
                new_text="y",
                expected_sha256="a" * 64,
                expected_occurrences=1,
            ),
        )


# ---------------------------------------------------------------------------
# Security — symlink rejection
# ---------------------------------------------------------------------------


def test_replace_text_rejects_symlink(forge_env: ForgeEnvironmentProtocol) -> None:
    replacer, repo, workspace_path, ws_id = _workspace_replacer(forge_env)

    target = workspace_path / "realfile.txt"
    _ = target.write_text("original content\n")
    (workspace_path / "link.txt").symlink_to("realfile.txt")

    with pytest.raises(SecurityError, match="symlink"):
        _ = replacer.execute(
            repo,
            workspace_path,
            WorkspaceReplaceTextCommand(
                workspace_id=ws_id,
                relative_path="link.txt",
                old_text="original",
                new_text="modified",
                expected_sha256="a" * 64,
                expected_occurrences=1,
            ),
        )

    # Target file must be unmodified
    assert target.read_text() == "original content\n"


# ---------------------------------------------------------------------------
# Security — binary file (NUL byte in content)
# ---------------------------------------------------------------------------


def test_replace_text_rejects_invalid_utf8(forge_env: ForgeEnvironmentProtocol) -> None:
    """Invalid UTF-8 bytes without NUL must be rejected with a typed SecurityError."""
    replacer, repo, workspace_path, ws_id = _workspace_replacer(forge_env)

    # \xff\xfe is invalid UTF-8 but neither byte is NUL.
    _ = (workspace_path / "bad-utf8.txt").write_bytes(b"\xff\xfe")
    sha = hashlib.sha256(b"\xff\xfe").hexdigest()

    with pytest.raises(SecurityError, match="not valid UTF-8"):
        _ = replacer.execute(
            repo,
            workspace_path,
            WorkspaceReplaceTextCommand(
                workspace_id=ws_id,
                relative_path="bad-utf8.txt",
                old_text="x",
                new_text="y",
                expected_sha256=sha,
                expected_occurrences=1,
            ),
        )


def test_replace_text_rejects_binary_file(forge_env: ForgeEnvironmentProtocol) -> None:
    replacer, repo, workspace_path, ws_id = _workspace_replacer(forge_env)

    _ = (workspace_path / "binary.bin").write_bytes(b"text\x00more")
    sha = hashlib.sha256(b"text\x00more").hexdigest()

    with pytest.raises(SecurityError, match="Binary files"):
        _ = replacer.execute(
            repo,
            workspace_path,
            WorkspaceReplaceTextCommand(
                workspace_id=ws_id,
                relative_path="binary.bin",
                old_text="text",
                new_text="data",
                expected_sha256=sha,
                expected_occurrences=1,
            ),
        )


# ---------------------------------------------------------------------------
# Security — NUL bytes in replacement text
# ---------------------------------------------------------------------------


def test_replace_text_rejects_nul_in_query(forge_env: ForgeEnvironmentProtocol) -> None:
    replacer, repo, workspace_path, ws_id = _workspace_replacer(forge_env)

    _ = (workspace_path / "clean.txt").write_text("clean text\n")

    with pytest.raises(SecurityError, match="NUL bytes"):
        _ = replacer.execute(
            repo,
            workspace_path,
            WorkspaceReplaceTextCommand(
                workspace_id=ws_id,
                relative_path="clean.txt",
                old_text="\x00bad",
                new_text="good",
                expected_sha256="a" * 64,
                expected_occurrences=1,
            ),
        )

    with pytest.raises(SecurityError, match="NUL bytes"):
        _ = replacer.execute(
            repo,
            workspace_path,
            WorkspaceReplaceTextCommand(
                workspace_id=ws_id,
                relative_path="clean.txt",
                old_text="clean",
                new_text="\x00bad",
                expected_sha256="a" * 64,
                expected_occurrences=1,
            ),
        )


# ---------------------------------------------------------------------------
# Security — oversized resulting content
# ---------------------------------------------------------------------------


def test_replace_text_rejects_oversized_result(
    forge_env: ForgeEnvironmentProtocol,
) -> None:
    replacer, repo, workspace_path, ws_id = _workspace_replacer(forge_env, max_file_bytes=100)

    # Original: 10 bytes; replacing 1 byte with 100 bytes would exceed limit
    original = "x" * 10
    _ = (workspace_path / "tiny.txt").write_text(original)
    sha = hashlib.sha256(original.encode("utf-8")).hexdigest()

    with pytest.raises(SecurityError, match="max_file_bytes"):
        _ = replacer.execute(
            repo,
            workspace_path,
            WorkspaceReplaceTextCommand(
                workspace_id=ws_id,
                relative_path="tiny.txt",
                old_text="x",
                new_text="y" * 200,
                expected_sha256=sha,
                expected_occurrences=10,
            ),
        )

    # Original file must be unmodified
    assert workspace_path.joinpath("tiny.txt").read_text() == original


# ---------------------------------------------------------------------------
# Security — old_text empty
# ---------------------------------------------------------------------------


def test_replace_text_rejects_empty_old_text(
    forge_env: ForgeEnvironmentProtocol,
) -> None:
    replacer, repo, workspace_path, ws_id = _workspace_replacer(forge_env)

    with pytest.raises(ValueError, match="old_text must be non-empty"):
        _ = replacer.execute(
            repo,
            workspace_path,
            WorkspaceReplaceTextCommand(
                workspace_id=ws_id,
                relative_path="any.txt",
                old_text="",
                new_text="something",
                expected_sha256="a" * 64,
                expected_occurrences=1,
            ),
        )


# ---------------------------------------------------------------------------
# Input validation — invalid SHA-256 format
# ---------------------------------------------------------------------------


def test_replace_text_rejects_invalid_sha_format(
    forge_env: ForgeEnvironmentProtocol,
) -> None:
    replacer, repo, workspace_path, ws_id = _workspace_replacer(forge_env)

    with pytest.raises(ValueError, match="expected_sha256"):
        _ = replacer.execute(
            repo,
            workspace_path,
            WorkspaceReplaceTextCommand(
                workspace_id=ws_id,
                relative_path="any.txt",
                old_text="x",
                new_text="y",
                expected_sha256="not-a-sha",
                expected_occurrences=1,
            ),
        )


# ---------------------------------------------------------------------------
# Input validation — expected_occurrences out of range
# ---------------------------------------------------------------------------


def test_replace_text_rejects_zero_occurrences(
    forge_env: ForgeEnvironmentProtocol,
) -> None:
    replacer, repo, workspace_path, ws_id = _workspace_replacer(forge_env)

    with pytest.raises(ValueError, match="expected_occurrences"):
        _ = replacer.execute(
            repo,
            workspace_path,
            WorkspaceReplaceTextCommand(
                workspace_id=ws_id,
                relative_path="any.txt",
                old_text="x",
                new_text="y",
                expected_sha256="a" * 64,
                expected_occurrences=0,
            ),
        )


def test_replace_text_rejects_oversized_occurrences(
    forge_env: ForgeEnvironmentProtocol,
) -> None:
    replacer, repo, workspace_path, ws_id = _workspace_replacer(forge_env)

    with pytest.raises(ValueError, match="expected_occurrences"):
        _ = replacer.execute(
            repo,
            workspace_path,
            WorkspaceReplaceTextCommand(
                workspace_id=ws_id,
                relative_path="any.txt",
                old_text="x",
                new_text="y",
                expected_sha256="a" * 64,
                expected_occurrences=1001,
            ),
        )


# ---------------------------------------------------------------------------
# File mode preservation on replacement
# ---------------------------------------------------------------------------


def test_replace_text_preserves_file_mode(
    forge_env: ForgeEnvironmentProtocol,
) -> None:
    replacer, repo, workspace_path, ws_id = _workspace_replacer(forge_env)

    exe = workspace_path / "script.sh"
    original = "#!/bin/sh\necho hi\n"
    _ = exe.write_text(original)
    exe.chmod(0o755)
    sha = hashlib.sha256(original.encode("utf-8")).hexdigest()

    _ = replacer.execute(
        repo,
        workspace_path,
        WorkspaceReplaceTextCommand(
            workspace_id=ws_id,
            relative_path="script.sh",
            old_text="hi",
            new_text="hello",
            expected_sha256=sha,
            expected_occurrences=1,
        ),
    )

    assert stat.S_IMODE(exe.stat().st_mode) == 0o755


# ---------------------------------------------------------------------------
# Error — file not found
# ---------------------------------------------------------------------------


def test_replace_text_rejects_missing_file(forge_env: ForgeEnvironmentProtocol) -> None:
    replacer, repo, workspace_path, ws_id = _workspace_replacer(forge_env)

    with pytest.raises(WorkspaceError, match="regular file"):
        _ = replacer.execute(
            repo,
            workspace_path,
            WorkspaceReplaceTextCommand(
                workspace_id=ws_id,
                relative_path="does-not-exist.txt",
                old_text="x",
                new_text="y",
                expected_sha256="a" * 64,
                expected_occurrences=1,
            ),
        )


# ---------------------------------------------------------------------------
# Error — file exceeds max_file_bytes
# ---------------------------------------------------------------------------


def test_replace_text_rejects_oversized_file(
    forge_env: ForgeEnvironmentProtocol,
) -> None:
    replacer, repo, workspace_path, ws_id = _workspace_replacer(forge_env, max_file_bytes=50)

    oversized = "x" * 51
    _ = (workspace_path / "big.txt").write_text(oversized)
    sha = hashlib.sha256(oversized.encode("utf-8")).hexdigest()

    with pytest.raises(SecurityError, match="max_file_bytes"):
        _ = replacer.execute(
            repo,
            workspace_path,
            WorkspaceReplaceTextCommand(
                workspace_id=ws_id,
                relative_path="big.txt",
                old_text="x",
                new_text="y",
                expected_sha256=sha,
                expected_occurrences=1,
            ),
        )
