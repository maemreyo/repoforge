"""Batched `workspace_replace_text` edits: one call, one lock, one fingerprint cycle.

Issue #142: 75% of `workspace_replace_text` calls happen in consecutive same-file runs
(up to 14 in a row). The `edits` parameter lets a caller submit a bounded ordered list of
exact replacements against one file in a single call, applied atomically under one
workspace lock. The pre-existing single-edit shape (`old_text`/`new_text`/
`expected_occurrences`) must keep behaving exactly as it did before this change.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import ForgeEnvironment

from repoforge.application.workspace.replace_text import MAX_EDITS_PER_CALL, TextEdit
from repoforge.domain.errors import SecurityError, WorkspaceError


def _hello_path(env: ForgeEnvironment, workspace_id: str) -> Path:
    status = env.service.workspace_status(workspace_id)
    return Path(status["path"]) / "hello.txt"


def test_single_edit_call_is_byte_for_byte_unchanged(forge_env: ForgeEnvironment) -> None:
    """A call without `edits` must return exactly the pre-existing result shape."""
    service = forge_env.service
    workspace_id = service.workspace_create("demo", "contract-stability")["workspace_id"]
    hello = service.workspace_read_file(workspace_id, "hello.txt")

    replaced = service.workspace_replace_text(
        workspace_id,
        "hello.txt",
        "hello",
        "changed hello",
        hello["sha256"],
    )

    assert set(replaced) == {
        "workspace_id",
        "path",
        "sha256",
        "replacements",
        "diff_stat",
        "workspace_fingerprint",
        "head_sha",
    }
    assert replaced["workspace_id"] == workspace_id
    assert replaced["path"] == "hello.txt"
    assert replaced["replacements"] == 1
    on_disk = _hello_path(forge_env, workspace_id).read_bytes()
    assert on_disk.decode("utf-8") == "changed hello\n"
    import hashlib

    assert replaced["sha256"] == hashlib.sha256(on_disk).hexdigest()


def test_single_edit_default_occurrences_and_errors_are_unchanged(
    forge_env: ForgeEnvironment,
) -> None:
    """Existing validation error text for the single-edit path must not shift."""
    service = forge_env.service
    workspace_id = service.workspace_create("demo", "contract-errors")["workspace_id"]
    hello = service.workspace_read_file(workspace_id, "hello.txt")

    with pytest.raises(ValueError, match="old_text must be non-empty"):
        service.workspace_replace_text(workspace_id, "hello.txt", "", "x", hello["sha256"])

    with pytest.raises(WorkspaceError, match="Expected 3 occurrences, found 1"):
        service.workspace_replace_text(workspace_id, "hello.txt", "hello", "x", hello["sha256"], 3)


def test_batch_edits_apply_sequentially_and_atomically(forge_env: ForgeEnvironment) -> None:
    service = forge_env.service
    workspace_id = service.workspace_create("demo", "batch-success")["workspace_id"]
    before_status = service.workspace_status(workspace_id)
    hello = service.workspace_read_file(workspace_id, "hello.txt")

    result = service.workspace_replace_text(
        workspace_id,
        "hello.txt",
        expected_sha256=hello["sha256"],
        edits=[
            TextEdit("hello", "hi there"),
            TextEdit("hi there", "hi there, again"),
        ],
    )

    assert result["replacements"] == 2
    assert result["edits"] == [
        {"index": 0, "replacements": 1},
        {"index": 1, "replacements": 1},
    ]
    on_disk = _hello_path(forge_env, workspace_id).read_bytes()
    assert on_disk.decode("utf-8") == "hi there, again\n"
    import hashlib

    assert result["sha256"] == hashlib.sha256(on_disk).hexdigest()
    assert result["workspace_fingerprint"] != before_status["workspace_fingerprint"]
    assert "head_sha" in result


def test_batch_failure_rejects_whole_call_and_leaves_file_untouched(
    forge_env: ForgeEnvironment,
) -> None:
    service = forge_env.service
    workspace_id = service.workspace_create("demo", "batch-atomic-reject")["workspace_id"]
    hello = service.workspace_read_file(workspace_id, "hello.txt")
    path = _hello_path(forge_env, workspace_id)
    before_bytes = path.read_bytes()
    before_status = service.workspace_status(workspace_id)

    with pytest.raises(WorkspaceError, match=r"edits\[1\]: expected 1 occurrences, found 0"):
        service.workspace_replace_text(
            workspace_id,
            "hello.txt",
            expected_sha256=hello["sha256"],
            edits=[
                TextEdit("hello", "hi there"),
                TextEdit("this text is not present", "does not matter"),
                TextEdit("hi there", "unreachable"),
            ],
        )

    after_bytes = path.read_bytes()
    after_status = service.workspace_status(workspace_id)
    assert after_bytes == before_bytes
    assert after_status["workspace_fingerprint"] == before_status["workspace_fingerprint"]
    assert after_status["head_sha"] == before_status["head_sha"]


def test_batch_rejects_more_than_max_edits_per_call(forge_env: ForgeEnvironment) -> None:
    service = forge_env.service
    workspace_id = service.workspace_create("demo", "batch-bound")["workspace_id"]
    hello = service.workspace_read_file(workspace_id, "hello.txt")
    path = _hello_path(forge_env, workspace_id)
    before_bytes = path.read_bytes()

    too_many = [TextEdit("hello", "hi") for _ in range(MAX_EDITS_PER_CALL + 1)]
    assert len(too_many) == 21

    with pytest.raises(ValueError, match=r"at most 20 entries, got 21"):
        service.workspace_replace_text(
            workspace_id,
            "hello.txt",
            expected_sha256=hello["sha256"],
            edits=too_many,
        )

    assert path.read_bytes() == before_bytes


def test_batch_rejects_empty_edits_list(forge_env: ForgeEnvironment) -> None:
    service = forge_env.service
    workspace_id = service.workspace_create("demo", "batch-empty")["workspace_id"]
    hello = service.workspace_read_file(workspace_id, "hello.txt")

    with pytest.raises(ValueError, match="at least one entry"):
        service.workspace_replace_text(
            workspace_id, "hello.txt", expected_sha256=hello["sha256"], edits=[]
        )


def test_batch_rejects_combining_top_level_text_with_edits(forge_env: ForgeEnvironment) -> None:
    service = forge_env.service
    workspace_id = service.workspace_create("demo", "batch-combo")["workspace_id"]
    hello = service.workspace_read_file(workspace_id, "hello.txt")

    with pytest.raises(ValueError, match="must not be provided together with edits"):
        service.workspace_replace_text(
            workspace_id,
            "hello.txt",
            "hello",
            "hi",
            hello["sha256"],
            edits=[TextEdit("hello", "hi")],
        )


def test_batch_rejects_nul_byte_in_entry_by_index(forge_env: ForgeEnvironment) -> None:
    service = forge_env.service
    workspace_id = service.workspace_create("demo", "batch-nul")["workspace_id"]
    hello = service.workspace_read_file(workspace_id, "hello.txt")

    with pytest.raises(SecurityError, match=r"edits\[0\]: NUL bytes"):
        service.workspace_replace_text(
            workspace_id,
            "hello.txt",
            expected_sha256=hello["sha256"],
            edits=[TextEdit("hello", "hi\x00there")],
        )


def test_batch_rejects_empty_old_text_in_entry_by_index(forge_env: ForgeEnvironment) -> None:
    service = forge_env.service
    workspace_id = service.workspace_create("demo", "batch-empty-old-text")["workspace_id"]
    hello = service.workspace_read_file(workspace_id, "hello.txt")

    with pytest.raises(ValueError, match=r"edits\[1\]: old_text must be non-empty"):
        service.workspace_replace_text(
            workspace_id,
            "hello.txt",
            expected_sha256=hello["sha256"],
            edits=[TextEdit("hello", "hi"), TextEdit("", "x")],
        )


def test_batch_rejects_stale_sha256_and_leaves_file_untouched(
    forge_env: ForgeEnvironment,
) -> None:
    service = forge_env.service
    workspace_id = service.workspace_create("demo", "batch-stale-sha")["workspace_id"]
    path = _hello_path(forge_env, workspace_id)
    before_bytes = path.read_bytes()
    before_status = service.workspace_status(workspace_id)

    with pytest.raises(WorkspaceError, match="File changed since it was read"):
        service.workspace_replace_text(
            workspace_id,
            "hello.txt",
            expected_sha256="0" * 64,
            edits=[TextEdit("hello", "hi")],
        )

    assert path.read_bytes() == before_bytes
    after_status = service.workspace_status(workspace_id)
    assert after_status["workspace_fingerprint"] == before_status["workspace_fingerprint"]
