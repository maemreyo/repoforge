from __future__ import annotations

from pathlib import Path

import pytest
from conftest import ForgeEnvironment, create_forge_environment

from repoforge.errors import SecurityError, WorkspaceError


def test_complete_service_tool_lifecycle(forge_env: ForgeEnvironment) -> None:
    service = forge_env.service

    listed = service.repo_list()
    assert listed["repositories"][0]["display_name"] == "Demo Repository"
    assert listed["repositories"][0]["default_verification_profile"] == "full"

    repo_status = service.repo_status("demo")
    assert repo_status["gh_authenticated"] is True
    assert "main" in repo_status["git_status"]

    context = service.repo_context("demo")
    assert context["package_manager"] == "pnpm@10.20.0"
    assert context["scripts"]["test"] == "echo test"
    assert any(item["path"] == "AGENTS.md" for item in context["instruction_files"])

    history = service.repo_recent_commits("demo", 3)
    assert history["commits"][0]["subject"] == "initial"
    assert service.repo_issue_read("demo", 7)["number"] == 7
    assert service.repo_pr_read("demo", 8)["number"] == 8

    created = service.workspace_create("demo", "Improve developer experience")
    workspace_id = created["workspace_id"]
    workspace_path = Path(created["path"])
    assert created["branch"].startswith("ai/improve-developer-experience-")
    assert service.workspace_list()["workspaces"][0]["workspace_id"] == workspace_id

    status = service.workspace_status(workspace_id)
    assert status["clean"] is True
    tree = service.workspace_tree(workspace_id, 100)
    assert "hello.txt" in tree["entries"]

    hello = service.workspace_read_file(workspace_id, "hello.txt")
    assert hello["content"] == "1: hello"
    batch = service.workspace_read_files(workspace_id, ["hello.txt", "README.md"])
    assert len(batch["files"]) == 2
    search = service.workspace_search(workspace_id, "Repository")
    assert search["matches"]

    replaced = service.workspace_replace_text(
        workspace_id,
        "hello.txt",
        "hello",
        "changed hello",
        hello["sha256"],
    )
    assert replaced["replacements"] == 1
    created_file = service.workspace_write_file(workspace_id, "notes.txt", "temporary\n", "<new>")
    assert created_file["path"] == "notes.txt"

    current_status = service.workspace_status(workspace_id)
    restored = service.workspace_restore_paths(
        workspace_id, ["notes.txt"], current_status["workspace_fingerprint"]
    )
    assert "notes.txt" in restored["removed_untracked"]
    assert not workspace_path.joinpath("notes.txt").exists()

    patch_status = service.workspace_status(workspace_id)
    patch = """diff --git a/README.md b/README.md
--- a/README.md
+++ b/README.md
@@ -1,3 +1,5 @@
 # Demo
 
 Repository instructions.
+
+Developer experience improved.
"""
    patched = service.workspace_apply_patch(
        workspace_id,
        patch,
        patch_status["head_sha"],
        patch_status["workspace_fingerprint"],
    )
    assert "README.md" in patched["changed_paths"]

    diff = service.workspace_diff(workspace_id)
    assert "Developer experience improved" in diff["diff"]
    assert diff["change_metrics"]["changed_files"] == 2

    quick = service.workspace_run_profile(workspace_id, "quick")
    assert quick["satisfies_commit_gate"] is False
    verified = service.workspace_verify(workspace_id)
    assert verified["satisfies_commit_gate"] is True

    committed = service.workspace_commit(workspace_id, "Improve developer experience")
    assert committed["head_sha"] != created["head_sha"]
    pushed = service.workspace_push(workspace_id)
    assert pushed["head_sha"] == committed["head_sha"]

    pr = service.workspace_create_draft_pr(
        workspace_id, "Improve developer experience", "## Summary\n\nSafer workflow."
    )
    assert pr["draft"] is True
    assert pr["labels"] == ["agent"]
    assert pr["reviewers"] == ["reviewer"]

    updated = service.workspace_update_draft_pr(workspace_id, title="Improve DX safely")
    assert updated["title"] == "Improve DX safely"
    pr_status = service.workspace_pr_status(workspace_id)
    assert pr_status["number"] == 42
    checks = service.workspace_pr_checks(workspace_id, required_only=True)
    assert checks["all_passed"] is True
    assert checks["summary"] == {"pass": 1, "skipping": 1}

    removed = service.workspace_remove(workspace_id, delete_local_branch=True)
    assert removed["removed"] is True
    assert removed["remote_branch_untouched"] is True


def test_batch_limit_and_denied_path(forge_env: ForgeEnvironment) -> None:
    service = forge_env.service
    workspace_id = service.workspace_create("demo", "negative tests")["workspace_id"]
    with pytest.raises(ValueError, match="max_batch_files"):
        service.workspace_read_files(workspace_id, ["hello.txt"] * 21)
    with pytest.raises(SecurityError):
        service.workspace_write_file(
            workspace_id, ".github/workflows/evil.yml", "name: evil\n", "<new>"
        )


def test_stale_locks_and_verification_invalidation(forge_env: ForgeEnvironment) -> None:
    service = forge_env.service
    workspace_id = service.workspace_create("demo", "stale lock")["workspace_id"]
    hello = service.workspace_read_file(workspace_id, "hello.txt")
    service.workspace_write_file(workspace_id, "hello.txt", "changed once\n", hello["sha256"])
    with pytest.raises(WorkspaceError, match="changed since"):
        service.workspace_write_file(workspace_id, "hello.txt", "changed twice\n", hello["sha256"])

    service.workspace_verify(workspace_id)
    current = service.workspace_read_file(workspace_id, "hello.txt")
    service.workspace_write_file(
        workspace_id, "hello.txt", "changed after verify\n", current["sha256"]
    )
    with pytest.raises(WorkspaceError, match="changed after verification"):
        service.workspace_commit(workspace_id, "Should fail")

    status = service.workspace_status(workspace_id)
    stale = status["workspace_fingerprint"]
    current = service.workspace_read_file(workspace_id, "hello.txt")
    service.workspace_write_file(workspace_id, "hello.txt", "changed again\n", current["sha256"])
    with pytest.raises(WorkspaceError, match="changed since"):
        service.workspace_restore_paths(workspace_id, ["hello.txt"], stale)


def test_change_budget_blocks_verification_and_commit(tmp_path: Path) -> None:
    env = create_forge_environment(tmp_path, max_changed_files=1, require_verification=False)
    service = env.service
    workspace_id = service.workspace_create("demo", "too broad")["workspace_id"]
    hello = service.workspace_read_file(workspace_id, "hello.txt")
    service.workspace_write_file(workspace_id, "hello.txt", "changed budget\n", hello["sha256"])
    service.workspace_write_file(workspace_id, "one.txt", "one\n", "<new>")
    with pytest.raises(WorkspaceError, match="Change budget exceeded"):
        service.workspace_verify(workspace_id)
    with pytest.raises(WorkspaceError, match="Change budget exceeded"):
        service.workspace_commit(workspace_id, "Too broad")
