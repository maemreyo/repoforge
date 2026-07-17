from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from conftest import ForgeEnvironment, create_forge_environment

from repoforge.application.workspace.mutate import (
    ApplyPatchMutation,
    CreateMutation,
    DeleteMutation,
    MoveMutation,
    ReplaceTextMutation,
    RestoreMutation,
    TextReplacement,
    WriteMutation,
)
from repoforge.domain.errors import SecurityError, WorkspaceError


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_mixed_mutations_commit_as_one_journaled_transaction(
    forge_env: ForgeEnvironment,
) -> None:
    service = forge_env.service
    workspace_id = service.workspace_create("demo", "v2 mixed mutate")["workspace_id"]
    root = Path(service.workspace_status(workspace_id)["path"])
    hello = service.workspace_read_file(workspace_id, "hello.txt")
    agents = service.workspace_read_file(workspace_id, "AGENTS.md")
    before = service.workspace_status(workspace_id)

    result = service.workspace_mutate(
        workspace_id,
        [
            ReplaceTextMutation(
                path="hello.txt",
                expected_sha256=hello["sha256"],
                edits=(TextReplacement("hello", "changed hello"),),
            ),
            CreateMutation(path="temporary.txt", content="temporary\n"),
            MoveMutation(
                source="temporary.txt",
                destination="moved.txt",
                expected_source_sha256=hashlib.sha256(b"temporary\n").hexdigest(),
            ),
            DeleteMutation(path="AGENTS.md", expected_sha256=agents["sha256"]),
        ],
        expected_workspace_fingerprint=before["workspace_fingerprint"],
    )

    assert result["changed"] is True
    assert result["dry_run"] is False
    assert result["operation_count"] == 4
    assert result["workspace_fingerprint"] != before["workspace_fingerprint"]
    assert result["head_sha"] == before["head_sha"]
    assert (root / "hello.txt").read_text(encoding="utf-8") == "changed hello\n"
    assert not (root / "temporary.txt").exists()
    assert (root / "moved.txt").read_text(encoding="utf-8") == "temporary\n"
    assert not (root / "AGENTS.md").exists()
    assert [item["status"] for item in result["operations"]] == [
        "ready",
        "ready",
        "ready",
        "ready",
    ]


def test_no_op_write_returns_changed_false_and_preserves_fingerprint(
    forge_env: ForgeEnvironment,
) -> None:
    service = forge_env.service
    workspace_id = service.workspace_create("demo", "v2 no op")["workspace_id"]
    hello = service.workspace_read_file(workspace_id, "hello.txt")
    status = service.workspace_status(workspace_id)

    result = service.workspace_mutate(
        workspace_id,
        [
            WriteMutation(
                path="hello.txt",
                content="hello\n",
                expected_sha256=hello["sha256"],
            )
        ],
        expected_workspace_fingerprint=status["workspace_fingerprint"],
    )

    assert result["changed"] is False
    assert result["workspace_fingerprint"] == status["workspace_fingerprint"]
    assert result["operations"][0]["status"] == "no_op"


def test_dry_run_reports_candidates_and_failure_without_mutating(
    forge_env: ForgeEnvironment,
) -> None:
    service = forge_env.service
    workspace_id = service.workspace_create("demo", "v2 dry run")["workspace_id"]
    root = Path(service.workspace_status(workspace_id)["path"])
    hello = service.workspace_read_file(workspace_id, "hello.txt")
    before = service.workspace_status(workspace_id)

    result = service.workspace_mutate(
        workspace_id,
        [
            ReplaceTextMutation(
                path="hello.txt",
                expected_sha256=hello["sha256"],
                edits=(TextReplacement("missing", "replacement"),),
            ),
            CreateMutation(path="would-create.txt", content="candidate\n"),
        ],
        expected_workspace_fingerprint=before["workspace_fingerprint"],
        dry_run=True,
    )

    assert result["dry_run"] is True
    assert result["changed"] is False
    assert result["would_change"] is True
    assert result["ready"] is False
    assert result["operations"][0]["status"] == "failed"
    assert "expected 1 occurrences, found 0" in result["operations"][0]["failure_reason"]
    assert result["operations"][1]["status"] == "ready"
    assert result["operations"][1]["after_sha256"] == hashlib.sha256(b"candidate\n").hexdigest()
    assert (root / "hello.txt").read_text(encoding="utf-8") == "hello\n"
    assert not (root / "would-create.txt").exists()
    assert (
        service.workspace_status(workspace_id)["workspace_fingerprint"]
        == before["workspace_fingerprint"]
    )


def test_stale_global_fingerprint_and_file_sha_leave_tree_untouched(
    forge_env: ForgeEnvironment,
) -> None:
    service = forge_env.service
    workspace_id = service.workspace_create("demo", "v2 stale mutate")["workspace_id"]
    root = Path(service.workspace_status(workspace_id)["path"])
    hello = service.workspace_read_file(workspace_id, "hello.txt")
    stale = service.workspace_status(workspace_id)["workspace_fingerprint"]
    service.workspace_write_file(
        workspace_id,
        "hello.txt",
        "changed outside mutate\n",
        hello["sha256"],
    )

    with pytest.raises(WorkspaceError, match="Workspace changed since"):
        service.workspace_mutate(
            workspace_id,
            [CreateMutation(path="new.txt", content="new\n")],
            expected_workspace_fingerprint=stale,
        )
    assert not (root / "new.txt").exists()

    current = service.workspace_status(workspace_id)
    with pytest.raises(WorkspaceError, match="expected_sha256"):
        service.workspace_mutate(
            workspace_id,
            [
                WriteMutation(
                    path="hello.txt",
                    content="another\n",
                    expected_sha256="0" * 64,
                ),
                CreateMutation(path="also-new.txt", content="new\n"),
            ],
            expected_workspace_fingerprint=current["workspace_fingerprint"],
        )
    assert (root / "hello.txt").read_text(encoding="utf-8") == "changed outside mutate\n"
    assert not (root / "also-new.txt").exists()


def test_patch_and_restore_are_planned_into_the_same_transaction_model(
    forge_env: ForgeEnvironment,
) -> None:
    service = forge_env.service
    workspace_id = service.workspace_create("demo", "v2 patch restore")["workspace_id"]
    root = Path(service.workspace_status(workspace_id)["path"])
    status = service.workspace_status(workspace_id)
    patch = """diff --git a/README.md b/README.md
--- a/README.md
+++ b/README.md
@@ -1,3 +1,4 @@
 # Demo
 
 Repository instructions.
+Journaled patch.
"""

    patched = service.workspace_mutate(
        workspace_id,
        [ApplyPatchMutation(patch=patch)],
        expected_workspace_fingerprint=status["workspace_fingerprint"],
    )
    assert patched["changed"] is True
    assert "Journaled patch." in (root / "README.md").read_text(encoding="utf-8")

    restored = service.workspace_mutate(
        workspace_id,
        [RestoreMutation(paths=("README.md",))],
        expected_workspace_fingerprint=patched["workspace_fingerprint"],
    )
    assert restored["changed"] is True
    assert (root / "README.md").read_text(encoding="utf-8") == (
        "# Demo\n\nRepository instructions.\n"
    )


def test_change_budget_failure_rolls_back_the_entire_transaction(tmp_path: Path) -> None:
    env = create_forge_environment(tmp_path, max_changed_files=1)
    service = env.service
    workspace_id = service.workspace_create("demo", "v2 budget rollback")["workspace_id"]
    root = Path(service.workspace_status(workspace_id)["path"])
    status = service.workspace_status(workspace_id)

    with pytest.raises(WorkspaceError, match="Change budget exceeded"):
        service.workspace_mutate(
            workspace_id,
            [
                CreateMutation(path="one.txt", content="one\n"),
                CreateMutation(path="two.txt", content="two\n"),
            ],
            expected_workspace_fingerprint=status["workspace_fingerprint"],
        )

    assert not (root / "one.txt").exists()
    assert not (root / "two.txt").exists()
    assert (
        service.workspace_status(workspace_id)["workspace_fingerprint"]
        == status["workspace_fingerprint"]
    )


def test_per_op_policy_gating_is_loaded_from_repository_config(tmp_path: Path) -> None:
    env = create_forge_environment(
        tmp_path,
        allowed_mutation_ops=("replace_text", "write"),
    )
    service = env.service
    workspace_id = service.workspace_create("demo", "v2 op policy")["workspace_id"]
    status = service.workspace_status(workspace_id)

    with pytest.raises(SecurityError, match=r"create.*disabled"):
        service.workspace_mutate(
            workspace_id,
            [CreateMutation(path="blocked.txt", content="blocked\n")],
            expected_workspace_fingerprint=status["workspace_fingerprint"],
        )


def test_denied_paths_and_binary_text_replacements_fail_closed(
    forge_env: ForgeEnvironment,
) -> None:
    service = forge_env.service
    workspace_id = service.workspace_create("demo", "v2 denied mutate")["workspace_id"]
    root = Path(service.workspace_status(workspace_id)["path"])
    status = service.workspace_status(workspace_id)

    with pytest.raises(SecurityError):
        service.workspace_mutate(
            workspace_id,
            [CreateMutation(path=".github/workflows/evil.yml", content="evil\n")],
            expected_workspace_fingerprint=status["workspace_fingerprint"],
        )

    (root / "binary.dat").write_bytes(b"before\x00after")
    binary_status = service.workspace_status(workspace_id)
    with pytest.raises(SecurityError, match="binary"):
        service.workspace_mutate(
            workspace_id,
            [
                ReplaceTextMutation(
                    path="binary.dat",
                    expected_sha256=_sha(root / "binary.dat"),
                    edits=(TextReplacement("before", "changed"),),
                )
            ],
            expected_workspace_fingerprint=binary_status["workspace_fingerprint"],
        )
