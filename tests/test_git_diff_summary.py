from __future__ import annotations

import subprocess

import pytest
from conftest import ForgeEnvironment

from repoforge.adapters.git.diff_summary import parse_diff_summary
from repoforge.domain.errors import CommandError


def test_parse_diff_summary_covers_statuses_rename_copy_type_and_binary() -> None:
    summaries = parse_diff_summary(
        b"M\0modified.txt\0A\0added.txt\0D\0deleted.txt\0"
        b"R100\0old name.txt\0new name.txt\0"
        b"C100\0copy source.txt\0copy target.txt\0T\0typed.txt\0",
        b"2\t1\tmodified.txt\0"
        b"3\t0\tadded.txt\0"
        b"0\t4\tdeleted.txt\0"
        b"5\t6\t\0old name.txt\0new name.txt\0"
        b"7\t0\t\0copy source.txt\0copy target.txt\0"
        b"-\t-\ttyped.txt\0",
    )

    assert [(item.path, item.status, item.additions, item.deletions) for item in summaries] == [
        ("added.txt", "added", 3, 0),
        ("copy target.txt", "added", 7, 0),
        ("deleted.txt", "deleted", 0, 4),
        ("modified.txt", "modified", 2, 1),
        ("new name.txt", "renamed", 5, 6),
        ("typed.txt", "modified", 0, 0),
    ]


@pytest.mark.parametrize(
    ("name_status", "numstat"),
    [
        (b"M\0", b""),
        (b"R100\0old.txt\0", b""),
        (b"M\0file.txt\0", b"not-a-count\t1\tfile.txt\0"),
        (b"M\0file.txt\0", b"1\t2"),
    ],
)
def test_parse_diff_summary_rejects_malformed_records(
    name_status: bytes,
    numstat: bytes,
) -> None:
    with pytest.raises(CommandError):
        parse_diff_summary(name_status, numstat)


def test_diff_summary_reports_tracked_and_untracked_changes(
    forge_env: ForgeEnvironment,
) -> None:
    service = forge_env.service
    workspace_id = service.workspace_create("demo", "git diff summary")["workspace_id"]
    _, repo, root = service._workspace_retrieval.ctx.workspace(workspace_id)
    (root / "hello.txt").write_text("changed\nsecond\n", encoding="utf-8")
    (root / "added.txt").write_text("added\n", encoding="utf-8")
    subprocess.run(["git", "add", "added.txt"], cwd=root, check=True)
    (root / "README.md").unlink()
    subprocess.run(["git", "mv", "AGENTS.md", "renamed.txt"], cwd=root, check=True)
    (root / "untracked.txt").write_text("one\ntwo\n", encoding="utf-8")
    (root / "binary.bin").write_bytes(b"\x00binary")

    summaries = service._workspace_retrieval.ctx.git.diff_summary(root, repo, staged=False)
    by_path = {item.path: item for item in summaries}

    assert by_path["hello.txt"].status == "modified"
    assert (by_path["hello.txt"].additions, by_path["hello.txt"].deletions) == (2, 1)
    assert by_path["added.txt"].status == "added"
    assert by_path["README.md"].status == "deleted"
    assert by_path["renamed.txt"].status == "renamed"
    assert by_path["untracked.txt"].additions == 2
    assert by_path["binary.bin"].additions == 0


def test_diff_summary_staged_mode_excludes_unstaged_and_untracked_changes(
    forge_env: ForgeEnvironment,
) -> None:
    service = forge_env.service
    workspace_id = service.workspace_create("demo", "staged git diff summary")["workspace_id"]
    _, repo, root = service._workspace_retrieval.ctx.workspace(workspace_id)
    (root / "staged.txt").write_text("staged\n", encoding="utf-8")
    subprocess.run(["git", "add", "staged.txt"], cwd=root, check=True)
    (root / "hello.txt").write_text("unstaged\n", encoding="utf-8")
    (root / "untracked.txt").write_text("untracked\n", encoding="utf-8")

    summaries = service._workspace_retrieval.ctx.git.diff_summary(root, repo, staged=True)

    assert [(item.path, item.status) for item in summaries] == [("staged.txt", "added")]
