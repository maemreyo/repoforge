"""`workspace_edit`: atomic exact-text replacement across one or more files.

Issue #142 established that `workspace_replace_text`'s batched `edits` shape lets a
caller submit a bounded ordered list of exact replacements against one file in a
single call. `workspace_edit` generalizes that to multiple files in one call: every
file's SHA and every edit's occurrence count are verified and applied to an in-memory
buffer before anything is written, so a mismatch anywhere in the call leaves the whole
workspace untouched.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from conftest import ForgeEnvironment

from repoforge.application.workspace.edit import (
    MAX_EDITS_PER_FILE,
    MAX_FILES_PER_CALL,
    FileEdit,
    TextEdit,
)
from repoforge.domain.errors import SecurityError, WorkspaceError


def _hello_path(env: ForgeEnvironment, workspace_id: str) -> Path:
    status = env.service.workspace_status(workspace_id)
    return Path(status["path"]) / "hello.txt"


def test_single_file_single_edit(forge_env: ForgeEnvironment) -> None:
    service = forge_env.service
    workspace_id = service.workspace_create("demo", "edit-single")["workspace_id"]
    hello = service.workspace_read_file(workspace_id, "hello.txt")

    result = service.workspace_edit(
        workspace_id,
        [FileEdit("hello.txt", hello["sha256"], (TextEdit("hello", "changed hello"),))],
    )

    assert set(result) == {
        "workspace_id",
        "files",
        "diff_stat",
        "workspace_fingerprint",
        "head_sha",
    }
    assert result["workspace_id"] == workspace_id
    assert result["files"] == [
        {
            "path": "hello.txt",
            "sha256": result["files"][0]["sha256"],
            "replacements": 1,
        }
    ]
    on_disk = _hello_path(forge_env, workspace_id).read_bytes()
    assert on_disk.decode("utf-8") == "changed hello\n"
    assert result["files"][0]["sha256"] == hashlib.sha256(on_disk).hexdigest()


def test_single_file_multiple_ordered_edits(forge_env: ForgeEnvironment) -> None:
    service = forge_env.service
    workspace_id = service.workspace_create("demo", "edit-ordered")["workspace_id"]
    before_status = service.workspace_status(workspace_id)
    hello = service.workspace_read_file(workspace_id, "hello.txt")

    result = service.workspace_edit(
        workspace_id,
        [
            FileEdit(
                "hello.txt",
                hello["sha256"],
                (
                    TextEdit("hello", "hi there"),
                    TextEdit("hi there", "hi there, again"),
                ),
            )
        ],
    )

    assert result["files"][0]["replacements"] == 2
    on_disk = _hello_path(forge_env, workspace_id).read_bytes()
    assert on_disk.decode("utf-8") == "hi there, again\n"
    assert result["files"][0]["sha256"] == hashlib.sha256(on_disk).hexdigest()
    assert result["workspace_fingerprint"] != before_status["workspace_fingerprint"]
    assert "head_sha" in result


def test_multi_file_happy_path(forge_env: ForgeEnvironment) -> None:
    service = forge_env.service
    workspace_id = service.workspace_create("demo", "edit-multi-file")["workspace_id"]
    status = service.workspace_status(workspace_id)
    root = Path(status["path"])
    (root / "second.txt").write_text("second file\n", encoding="utf-8")

    hello = service.workspace_read_file(workspace_id, "hello.txt")
    second = service.workspace_read_file(workspace_id, "second.txt")

    result = service.workspace_edit(
        workspace_id,
        [
            FileEdit("hello.txt", hello["sha256"], (TextEdit("hello", "hi"),)),
            FileEdit("second.txt", second["sha256"], (TextEdit("second", "2nd"),)),
        ],
    )

    assert {entry["path"] for entry in result["files"]} == {"hello.txt", "second.txt"}
    assert (root / "hello.txt").read_text(encoding="utf-8") == "hi\n"
    assert (root / "second.txt").read_text(encoding="utf-8") == "2nd file\n"


def test_multi_file_failure_is_atomic_across_files(forge_env: ForgeEnvironment) -> None:
    service = forge_env.service
    workspace_id = service.workspace_create("demo", "edit-multi-atomic")["workspace_id"]
    status = service.workspace_status(workspace_id)
    root = Path(status["path"])
    (root / "second.txt").write_text("second file\n", encoding="utf-8")

    hello = service.workspace_read_file(workspace_id, "hello.txt")
    second = service.workspace_read_file(workspace_id, "second.txt")
    before_hello = (root / "hello.txt").read_bytes()
    before_second = (root / "second.txt").read_bytes()
    before_status = service.workspace_status(workspace_id)

    with pytest.raises(WorkspaceError, match="expected 1 occurrences, found 0"):
        service.workspace_edit(
            workspace_id,
            [
                FileEdit("hello.txt", hello["sha256"], (TextEdit("hello", "hi"),)),
                FileEdit(
                    "second.txt",
                    second["sha256"],
                    (TextEdit("this text is not present", "does not matter"),),
                ),
            ],
        )

    assert (root / "hello.txt").read_bytes() == before_hello
    assert (root / "second.txt").read_bytes() == before_second
    after_status = service.workspace_status(workspace_id)
    assert after_status["workspace_fingerprint"] == before_status["workspace_fingerprint"]
    assert after_status["head_sha"] == before_status["head_sha"]


def test_rejects_duplicate_paths(forge_env: ForgeEnvironment) -> None:
    service = forge_env.service
    workspace_id = service.workspace_create("demo", "edit-duplicate-path")["workspace_id"]
    hello = service.workspace_read_file(workspace_id, "hello.txt")
    path = _hello_path(forge_env, workspace_id)
    before_bytes = path.read_bytes()

    with pytest.raises(ValueError, match="duplicate path"):
        service.workspace_edit(
            workspace_id,
            [
                FileEdit("hello.txt", hello["sha256"], (TextEdit("hello", "hi"),)),
                FileEdit("hello.txt", hello["sha256"], (TextEdit("hi", "hey"),)),
            ],
        )

    assert path.read_bytes() == before_bytes


def test_rejects_more_than_max_files_per_call(forge_env: ForgeEnvironment) -> None:
    service = forge_env.service
    workspace_id = service.workspace_create("demo", "edit-too-many-files")["workspace_id"]
    hello = service.workspace_read_file(workspace_id, "hello.txt")

    too_many = [
        FileEdit(f"missing-{i}.txt", hello["sha256"], (TextEdit("x", "y"),))
        for i in range(MAX_FILES_PER_CALL + 1)
    ]
    assert len(too_many) == 21

    with pytest.raises(ValueError, match=r"at most 20 entries, got 21"):
        service.workspace_edit(workspace_id, too_many)


def test_rejects_more_than_max_edits_per_file(forge_env: ForgeEnvironment) -> None:
    service = forge_env.service
    workspace_id = service.workspace_create("demo", "edit-too-many-edits")["workspace_id"]
    hello = service.workspace_read_file(workspace_id, "hello.txt")
    path = _hello_path(forge_env, workspace_id)
    before_bytes = path.read_bytes()

    too_many_edits = tuple(TextEdit("hello", "hi") for _ in range(MAX_EDITS_PER_FILE + 1))
    assert len(too_many_edits) == 21

    with pytest.raises(ValueError, match=r"at most 20 entries, got 21"):
        service.workspace_edit(workspace_id, [FileEdit("hello.txt", hello["sha256"], too_many_edits)])

    assert path.read_bytes() == before_bytes


def test_rejects_empty_files_list(forge_env: ForgeEnvironment) -> None:
    service = forge_env.service
    workspace_id = service.workspace_create("demo", "edit-empty-files")["workspace_id"]

    with pytest.raises(ValueError, match="at least one entry"):
        service.workspace_edit(workspace_id, [])


def test_rejects_empty_edits_list_for_a_file(forge_env: ForgeEnvironment) -> None:
    service = forge_env.service
    workspace_id = service.workspace_create("demo", "edit-empty-edits")["workspace_id"]
    hello = service.workspace_read_file(workspace_id, "hello.txt")

    with pytest.raises(ValueError, match="at least one entry"):
        service.workspace_edit(workspace_id, [FileEdit("hello.txt", hello["sha256"], ())])


def test_rejects_nul_byte_in_edit_by_index(forge_env: ForgeEnvironment) -> None:
    service = forge_env.service
    workspace_id = service.workspace_create("demo", "edit-nul")["workspace_id"]
    hello = service.workspace_read_file(workspace_id, "hello.txt")

    with pytest.raises(SecurityError, match=r"edits\[0\]: NUL bytes"):
        service.workspace_edit(
            workspace_id,
            [FileEdit("hello.txt", hello["sha256"], (TextEdit("hello", "hi\x00there"),))],
        )


def test_rejects_empty_old_text_in_edit_by_index(forge_env: ForgeEnvironment) -> None:
    service = forge_env.service
    workspace_id = service.workspace_create("demo", "edit-empty-old-text")["workspace_id"]
    hello = service.workspace_read_file(workspace_id, "hello.txt")

    with pytest.raises(ValueError, match=r"edits\[1\]: old_text must be non-empty"):
        service.workspace_edit(
            workspace_id,
            [
                FileEdit(
                    "hello.txt",
                    hello["sha256"],
                    (TextEdit("hello", "hi"), TextEdit("", "x")),
                )
            ],
        )


def test_rejects_stale_sha256_and_leaves_file_untouched(forge_env: ForgeEnvironment) -> None:
    service = forge_env.service
    workspace_id = service.workspace_create("demo", "edit-stale-sha")["workspace_id"]
    path = _hello_path(forge_env, workspace_id)
    before_bytes = path.read_bytes()
    before_status = service.workspace_status(workspace_id)

    with pytest.raises(WorkspaceError, match="File changed since it was read"):
        service.workspace_edit(
            workspace_id,
            [FileEdit("hello.txt", "0" * 64, (TextEdit("hello", "hi"),))],
        )

    assert path.read_bytes() == before_bytes
    after_status = service.workspace_status(workspace_id)
    assert after_status["workspace_fingerprint"] == before_status["workspace_fingerprint"]
