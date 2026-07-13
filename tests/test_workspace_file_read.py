"""Tests for the WorkspaceFileReader typed application use case."""

from __future__ import annotations

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
from repoforge.workspace_file_read import (
    WorkspaceFileReadCommand,
    WorkspaceFileReader,
    WorkspaceFileReadPorts,
)


class ForgeEnvironmentProtocol(Protocol):
    """Minimal protocol describing what this test module accesses from forge_env."""

    config_path: Path


def _workspace_reader(
    forge_env: ForgeEnvironmentProtocol,
    *,
    max_file_bytes: int | None = None,
    max_tool_output_chars: int | None = None,
) -> tuple[WorkspaceFileReader, RepositoryConfig, Path, str]:
    """Create a real workspace and return (reader, repo, workspace_path, workspace_id)."""
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
    plan = creator.plan(repo, WorkspaceCreateCommand("demo", "file-read-test"))
    created = creator.execute(repo, plan)

    reader = WorkspaceFileReader(
        WorkspaceFileReadPorts(
            max_file_bytes=max_file_bytes or config.server.max_file_bytes,
            max_tool_output_chars=max_tool_output_chars or config.server.max_tool_output_chars,
        )
    )
    return reader, repo, created.path, created.workspace_id


# ---------------------------------------------------------------------------
# Security — path traversal rejection
# ---------------------------------------------------------------------------


def test_workspace_file_reader_rejects_traversal(
    forge_env: ForgeEnvironmentProtocol,
) -> None:
    reader, repo, workspace_path, ws_id = _workspace_reader(forge_env)

    with pytest.raises(SecurityError, match="normalized"):
        reader.execute(
            repo,
            workspace_path,
            WorkspaceFileReadCommand(workspace_id=ws_id, relative_path="../outside.txt"),
        )


# ---------------------------------------------------------------------------
# Happy path — basic file read
# ---------------------------------------------------------------------------


def test_workspace_file_reader_basic_read(forge_env: ForgeEnvironmentProtocol) -> None:
    reader, repo, workspace_path, ws_id = _workspace_reader(forge_env)

    # Given: a git-tracked file exists
    (workspace_path / "readme.txt").write_text("hello\nworld\n")

    # When: reading the file
    result = reader.execute(
        repo,
        workspace_path,
        WorkspaceFileReadCommand(workspace_id=ws_id, relative_path="readme.txt"),
    )

    # Then: full content with line numbers
    assert result.path == "readme.txt"
    assert result.content == "1: hello\n2: world"
    assert result.total_lines == 2
    assert result.size_bytes == len(b"hello\nworld\n")
    assert len(result.sha256) == 64
    assert result.start_line == 1
    assert result.end_line == 2
    assert result.truncated is False
    assert result.workspace_id == ws_id


# ---------------------------------------------------------------------------
# Line range — bounded start_line / end_line
# ---------------------------------------------------------------------------


def test_workspace_file_reader_line_range(forge_env: ForgeEnvironmentProtocol) -> None:
    reader, repo, workspace_path, ws_id = _workspace_reader(forge_env)

    text = "\n".join(f"line {i}" for i in range(1, 101))
    (workspace_path / "hundred.txt").write_text(text + "\n")

    result = reader.execute(
        repo,
        workspace_path,
        WorkspaceFileReadCommand(
            workspace_id=ws_id, relative_path="hundred.txt", start_line=10, end_line=12
        ),
    )

    assert result.start_line == 10
    assert result.end_line == 12
    assert result.content == "10: line 10\n11: line 11\n12: line 12"
    assert result.total_lines == 100


def test_workspace_file_reader_line_range_clamps_end(forge_env: ForgeEnvironmentProtocol) -> None:
    """end_line is clamped past EOF to the last line."""
    reader, repo, workspace_path, ws_id = _workspace_reader(forge_env)

    (workspace_path / "short.txt").write_text("only\nthree\nlines\n")

    result = reader.execute(
        repo,
        workspace_path,
        WorkspaceFileReadCommand(
            workspace_id=ws_id, relative_path="short.txt", start_line=1, end_line=999
        ),
    )

    assert result.end_line == 3
    assert result.total_lines == 3


def test_workspace_file_reader_empty_file(forge_env: ForgeEnvironmentProtocol) -> None:
    """Reading a zero-byte file returns empty content and zero totals."""
    reader, repo, workspace_path, ws_id = _workspace_reader(forge_env)

    (workspace_path / "empty.txt").write_text("")

    result = reader.execute(
        repo,
        workspace_path,
        WorkspaceFileReadCommand(workspace_id=ws_id, relative_path="empty.txt"),
    )

    assert result.content == ""
    assert result.total_lines == 0
    assert result.size_bytes == 0
    assert result.start_line == 1
    assert result.end_line == 0
    assert result.truncated is False


def test_workspace_file_reader_clamps_start_line_zero(
    forge_env: ForgeEnvironmentProtocol,
) -> None:
    """start_line=0 is clamped to 1 so it does not select from the end."""
    reader, repo, workspace_path, ws_id = _workspace_reader(forge_env)

    (workspace_path / "three.txt").write_text("alpha\nbeta\ngamma\n")

    result = reader.execute(
        repo,
        workspace_path,
        WorkspaceFileReadCommand(
            workspace_id=ws_id, relative_path="three.txt", start_line=0, end_line=2
        ),
    )

    assert result.start_line == 1
    assert result.content == "1: alpha\n2: beta"


def test_workspace_file_reader_clamps_end_line_window(
    forge_env: ForgeEnvironmentProtocol,
) -> None:
    """end_line past start_line+2000 is capped to a 2000-line window."""
    reader, repo, workspace_path, ws_id = _workspace_reader(forge_env)

    lines = "\n".join(f"line {i}" for i in range(5000))
    (workspace_path / "big.txt").write_text(lines + "\n")

    result = reader.execute(
        repo,
        workspace_path,
        WorkspaceFileReadCommand(
            workspace_id=ws_id, relative_path="big.txt", start_line=100, end_line=99999
        ),
    )

    # start_line(100) + 2000 = 2100; file lines are 0-indexed (range(5000))
    assert result.start_line == 100
    assert result.end_line == 2100
    assert result.total_lines == 5000
    assert result.content.startswith("100: line 99\n101: line 100")
    assert result.content.endswith("2100: line 2099")


def test_workspace_file_reader_line_range_start_beyond_eof(
    forge_env: ForgeEnvironmentProtocol,
) -> None:
    """start_line past EOF returns empty content."""
    reader, repo, workspace_path, ws_id = _workspace_reader(forge_env)

    (workspace_path / "tiny.txt").write_text("one\n")

    result = reader.execute(
        repo,
        workspace_path,
        WorkspaceFileReadCommand(
            workspace_id=ws_id, relative_path="tiny.txt", start_line=10, end_line=20
        ),
    )

    assert result.content == ""
    assert result.total_lines == 1
    assert result.end_line == 1


# ---------------------------------------------------------------------------
# Error — file not found
# ---------------------------------------------------------------------------


def test_workspace_file_reader_file_not_found(
    forge_env: ForgeEnvironmentProtocol,
) -> None:
    reader, repo, workspace_path, ws_id = _workspace_reader(forge_env)

    with pytest.raises(WorkspaceError, match="File not found"):
        reader.execute(
            repo,
            workspace_path,
            WorkspaceFileReadCommand(workspace_id=ws_id, relative_path="does-not-exist.txt"),
        )


# ---------------------------------------------------------------------------
# Security — symlink rejection
# ---------------------------------------------------------------------------


def test_workspace_file_reader_rejects_symlink(
    forge_env: ForgeEnvironmentProtocol,
) -> None:
    reader, repo, workspace_path, ws_id = _workspace_reader(forge_env)

    target = workspace_path / "realfile.txt"
    target.write_text("real content\n")
    (workspace_path / "link.txt").symlink_to("realfile.txt")

    with pytest.raises(SecurityError, match="symlink"):
        reader.execute(
            repo,
            workspace_path,
            WorkspaceFileReadCommand(workspace_id=ws_id, relative_path="link.txt"),
        )


# ---------------------------------------------------------------------------
# Security — binary file (NUL byte)
# ---------------------------------------------------------------------------


def test_workspace_file_reader_rejects_binary(
    forge_env: ForgeEnvironmentProtocol,
) -> None:
    reader, repo, workspace_path, ws_id = _workspace_reader(forge_env)

    (workspace_path / "binary.bin").write_bytes(b"text\x00more")

    with pytest.raises(SecurityError, match="Binary files"):
        reader.execute(
            repo,
            workspace_path,
            WorkspaceFileReadCommand(workspace_id=ws_id, relative_path="binary.bin"),
        )


# ---------------------------------------------------------------------------
# Security — invalid UTF-8
# ---------------------------------------------------------------------------


def test_workspace_file_reader_rejects_invalid_utf8(
    forge_env: ForgeEnvironmentProtocol,
) -> None:
    reader, repo, workspace_path, ws_id = _workspace_reader(forge_env)

    (workspace_path / "bad-utf8.bin").write_bytes(b"valid\xe9\xff\xfe")

    with pytest.raises(SecurityError, match="not valid UTF-8"):
        reader.execute(
            repo,
            workspace_path,
            WorkspaceFileReadCommand(workspace_id=ws_id, relative_path="bad-utf8.bin"),
        )


# ---------------------------------------------------------------------------
# Security — file exceeds max_file_bytes
# ---------------------------------------------------------------------------


def test_workspace_file_reader_rejects_oversized(
    forge_env: ForgeEnvironmentProtocol,
) -> None:
    reader, repo, workspace_path, ws_id = _workspace_reader(forge_env, max_file_bytes=50)

    (workspace_path / "big.txt").write_text("x" * 51 + "\n")

    with pytest.raises(SecurityError, match="max_file_bytes"):
        reader.execute(
            repo,
            workspace_path,
            WorkspaceFileReadCommand(workspace_id=ws_id, relative_path="big.txt"),
        )


# ---------------------------------------------------------------------------
# Content truncation — long content
# ---------------------------------------------------------------------------


def test_workspace_file_reader_truncates_long_content(
    forge_env: ForgeEnvironmentProtocol,
) -> None:
    """Truncation at configured max_tool_output_chars bounds output well below raw content."""
    reader, repo, workspace_path, ws_id = _workspace_reader(forge_env, max_tool_output_chars=50)

    # Raw content after line numbering will be ~1005 chars
    long_line = "A" * 1000 + "\n"
    (workspace_path / "long.txt").write_text(long_line)

    result = reader.execute(
        repo,
        workspace_path,
        WorkspaceFileReadCommand(workspace_id=ws_id, relative_path="long.txt"),
    )

    assert result.truncated is True
    assert "characters omitted" in result.content
    # Truncation should keep output far below 1000 chars (~87 max for limit=50)
    assert len(result.content) < 150


# ---------------------------------------------------------------------------
# Security — denied path by repository policy
# ---------------------------------------------------------------------------


def test_workspace_file_reader_rejects_denied_path(
    forge_env: ForgeEnvironmentProtocol,
) -> None:
    reader, repo, workspace_path, ws_id = _workspace_reader(forge_env)

    # Create the directory structure so the file exists
    denied_dir = workspace_path / ".github" / "workflows"
    denied_dir.mkdir(parents=True)
    (denied_dir / "evil.yml").write_text("name: ci\n")

    with pytest.raises(SecurityError, match="denied"):
        reader.execute(
            repo,
            workspace_path,
            WorkspaceFileReadCommand(
                workspace_id=ws_id, relative_path=".github/workflows/evil.yml"
            ),
        )
