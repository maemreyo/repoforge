from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import ForgeEnvironment, create_forge_environment, git

from repoforge.application.workspace.edit import FileEdit, TextEdit
from repoforge.domain.errors import (
    CommandError,
    ConfigError,
    ErrorCode,
    SecurityError,
    WorkspaceError,
)


def _audit_events(root: Path, action: str) -> list[dict[str, object]]:
    audit_path = root / "state" / "audit.jsonl"
    events = [
        json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines() if line
    ]
    return [event for event in events if event["action"] == action]


def test_workspace_write_file_replays_keyed_result_and_rejects_conflict(
    forge_env: ForgeEnvironment,
) -> None:
    service = forge_env.service
    workspace_id = service.workspace_create("demo", "keyed-write")["workspace_id"]

    first = service.workspace_write_file(
        workspace_id,
        "keyed.txt",
        "first\n",
        "<new>",
        idempotency_key="workspace-write-key-0001",
    )
    replayed = service.workspace_write_file(
        workspace_id,
        "keyed.txt",
        "first\n",
        "<new>",
        idempotency_key="workspace-write-key-0001",
    )

    assert replayed == first
    with pytest.raises(ConfigError) as conflict:
        service.workspace_write_file(
            workspace_id,
            "keyed.txt",
            "different\n",
            "<new>",
            idempotency_key="workspace-write-key-0001",
        )
    assert conflict.value.code is ErrorCode.IDEMPOTENCY_CONFLICT


def test_workspace_edit_replays_keyed_result_and_rejects_conflict(
    forge_env: ForgeEnvironment,
) -> None:
    service = forge_env.service
    workspace_id = service.workspace_create("demo", "keyed-edit")["workspace_id"]
    hello = service.workspace_read_file(workspace_id, "hello.txt")
    files = [FileEdit("hello.txt", hello["sha256"], (TextEdit("hello", "keyed edit"),))]

    first = service.workspace_edit(
        workspace_id,
        files,
        idempotency_key="workspace-edit-key-0001",
    )
    replayed = service.workspace_edit(
        workspace_id,
        [FileEdit("./hello.txt", hello["sha256"], (TextEdit("hello", "keyed edit"),))],
        idempotency_key="workspace-edit-key-0001",
    )

    assert replayed == first
    with pytest.raises(ConfigError) as conflict:
        service.workspace_edit(
            workspace_id,
            [FileEdit("hello.txt", hello["sha256"], (TextEdit("hello", "different"),))],
            idempotency_key="workspace-edit-key-0001",
        )
    assert conflict.value.code is ErrorCode.IDEMPOTENCY_CONFLICT


def test_workspace_apply_patch_replays_keyed_result_and_rejects_conflict(
    forge_env: ForgeEnvironment,
) -> None:
    service = forge_env.service
    workspace_id = service.workspace_create("demo", "keyed-patch")["workspace_id"]
    status = service.workspace_status(workspace_id)
    patch = """diff --git a/hello.txt b/hello.txt
--- a/hello.txt
+++ b/hello.txt
@@ -1 +1 @@
-hello
+keyed patch
"""

    first = service.workspace_apply_patch(
        workspace_id,
        patch,
        status["head_sha"],
        status["workspace_fingerprint"],
        idempotency_key="workspace-patch-key-0001",
    )
    replayed = service.workspace_apply_patch(
        workspace_id,
        patch,
        status["head_sha"],
        status["workspace_fingerprint"],
        idempotency_key="workspace-patch-key-0001",
    )

    assert replayed == first
    with pytest.raises(ConfigError) as conflict:
        service.workspace_apply_patch(
            workspace_id,
            patch.replace("keyed patch", "different patch"),
            status["head_sha"],
            status["workspace_fingerprint"],
            idempotency_key="workspace-patch-key-0001",
        )
    assert conflict.value.code is ErrorCode.IDEMPOTENCY_CONFLICT


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
    base_status = service.workspace_base_status(workspace_id)
    assert base_status["staleness"] == "current"
    refresh_preview = service.workspace_refresh_preview(
        workspace_id,
        status["head_sha"],
        status["workspace_fingerprint"],
    )
    refreshed = service.workspace_refresh(
        workspace_id,
        refresh_preview["preview_id"],
        status["head_sha"],
        status["workspace_fingerprint"],
    )
    assert refreshed["status"] == "current"
    tree = service.workspace_tree(workspace_id, 100)
    assert "hello.txt" in tree["entries"]

    hello = service.workspace_read_file(workspace_id, "hello.txt")
    assert hello["content"] == "1: hello"
    batch = service.workspace_read_files(workspace_id, ["hello.txt", "README.md"])
    assert len(batch["files"]) == 2
    search = service.workspace_search(workspace_id, "Repository")
    assert search["matches"]

    replaced = service.workspace_edit(
        workspace_id,
        [FileEdit("hello.txt", hello["sha256"], (TextEdit("hello", "changed hello"),))],
    )
    assert replaced["files"][0]["replacements"] == 1
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
    verified = service.workspace_run_profile(workspace_id)
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


def test_run_profile_default_and_verify_alias_share_canonical_contract(
    forge_env: ForgeEnvironment,
) -> None:
    service = forge_env.service
    canonical_workspace = service.workspace_create("demo", "canonical verification")["workspace_id"]
    alias_workspace = service.workspace_create("demo", "legacy verification alias")["workspace_id"]

    for workspace_id in (canonical_workspace, alias_workspace):
        current = service.workspace_read_file(workspace_id, "hello.txt")
        service.workspace_write_file(
            workspace_id,
            "hello.txt",
            "changed for verification parity\n",
            current["sha256"],
        )

    canonical = service.workspace_run_profile(canonical_workspace)
    alias = service.workspace_verify(alias_workspace)

    assert canonical["used_default"] is True
    assert canonical["repo_id"] == "demo"
    for key in (
        "profile",
        "description",
        "verification",
        "satisfies_commit_gate",
        "used_default",
        "repo_id",
        "working_directory",
    ):
        assert alias[key] == canonical[key]
    assert len(alias["commands"]) == len(canonical["commands"])
    for alias_command, canonical_command in zip(
        alias["commands"], canonical["commands"], strict=True
    ):
        assert alias_command["duration_ms"] >= 0
        assert canonical_command["duration_ms"] >= 0
        assert alias_command["cumulative_duration_ms"] >= alias_command["duration_ms"]
        assert canonical_command["cumulative_duration_ms"] >= canonical_command["duration_ms"]
        assert {
            key: value
            for key, value in alias_command.items()
            if key not in {"duration_ms", "cumulative_duration_ms"}
        } == {
            key: value
            for key, value in canonical_command.items()
            if key not in {"duration_ms", "cumulative_duration_ms"}
        }
    assert len(_audit_events(forge_env.root, "workspace_run_profile")) == 2
    assert _audit_events(forge_env.root, "workspace_verify") == []


@pytest.mark.parametrize(
    ("failure", "expected_code"),
    [
        ("missing_path", ErrorCode.WORKSPACE_PATH_MISSING),
        ("missing_git", ErrorCode.WORKTREE_REGISTRATION_STALE),
        ("branch_mismatch", ErrorCode.WORKSPACE_BRANCH_MISMATCH),
        ("outside_root", ErrorCode.WORKSPACE_OUTSIDE_ROOT),
    ],
)
def test_workspace_invariant_failures_have_specific_error_codes(
    forge_env: ForgeEnvironment, failure: str, expected_code: ErrorCode
) -> None:
    service = forge_env.service
    created = service.workspace_create("demo", f"workspace invariant {failure}")
    workspace_id = created["workspace_id"]
    workspace_path = Path(created["path"])
    context = service.application.context

    if failure == "missing_path":
        workspace_path.rename(workspace_path.with_name(workspace_path.name + "-moved"))
    elif failure == "missing_git":
        workspace_path.joinpath(".git").unlink()
    elif failure == "branch_mismatch":
        git("checkout", "-b", "ai/unexpected-runtime-branch", cwd=workspace_path)
    else:
        record = context.store.load(workspace_id)
        record.path = str(forge_env.root / "outside-workspace-root")
        context.store.save(record)

    with pytest.raises(WorkspaceError) as excinfo:
        context.workspace(workspace_id)
    assert excinfo.value.code is expected_code


def test_batch_limit_and_denied_path(forge_env: ForgeEnvironment) -> None:
    service = forge_env.service
    workspace_id = service.workspace_create("demo", "negative tests")["workspace_id"]
    with pytest.raises(ValueError, match="max_batch_files"):
        service.workspace_read_files(workspace_id, ["hello.txt"] * 21)
    with pytest.raises(SecurityError):
        service.workspace_write_file(
            workspace_id, ".github/workflows/evil.yml", "name: evil\n", "<new>"
        )


def test_workspace_search_context_lines_returns_surrounding_lines(
    forge_env: ForgeEnvironment,
) -> None:
    service = forge_env.service
    workspace_id = service.workspace_create("demo", "context search")["workspace_id"]
    workspace_path = Path(service.workspace_status(workspace_id)["path"])
    (workspace_path / "ctx.txt").write_text(
        "alpha\nbravo\nNEEDLE charlie\ndelta\necho\n", encoding="utf-8"
    )

    result = service.workspace_search(workspace_id, "NEEDLE", context_lines=2)
    assert result["matches"] == [
        "ctx.txt-1-alpha",
        "ctx.txt-2-bravo",
        "ctx.txt:3:NEEDLE charlie",
        "ctx.txt-4-delta",
        "ctx.txt-5-echo",
    ]
    assert result["truncated"] is False


def test_workspace_search_context_lines_bounds_and_truncation(
    forge_env: ForgeEnvironment,
) -> None:
    service = forge_env.service
    workspace_id = service.workspace_create("demo", "context bounds")["workspace_id"]
    workspace_path = Path(service.workspace_status(workspace_id)["path"])
    (workspace_path / "ctx2.txt").write_text("line1\nNEEDLE x\nline3\n", encoding="utf-8")

    with pytest.raises(ValueError, match="context_lines"):
        service.workspace_search(workspace_id, "NEEDLE", context_lines=6)
    with pytest.raises(ValueError, match="context_lines"):
        service.workspace_search(workspace_id, "NEEDLE", context_lines=-1)

    truncated = service.workspace_search(
        workspace_id, "NEEDLE", context_lines=1, max_results=2, path_glob="ctx2.txt"
    )
    assert truncated["matches"] == ["ctx2.txt-1-line1", "ctx2.txt:2:NEEDLE x"]
    assert truncated["truncated"] is True


def test_workspace_search_context_lines_never_leaks_denied_path(
    forge_env: ForgeEnvironment,
) -> None:
    service = forge_env.service
    workspace_id = service.workspace_create("demo", "context denied boundary")["workspace_id"]
    workspace_path = Path(service.workspace_status(workspace_id)["path"])
    (workspace_path / ".env").write_text(
        "before secret\nNEEDLE_BOUNDARY=denied\nafter secret\n", encoding="utf-8"
    )
    (workspace_path / "allowed_neighbor.txt").write_text(
        "line one\nNEEDLE_BOUNDARY here\nline three\n", encoding="utf-8"
    )

    result = service.workspace_search(workspace_id, "NEEDLE_BOUNDARY", context_lines=1)
    assert all(".env" not in match for match in result["matches"])
    assert all("secret" not in match for match in result["matches"])
    assert result["matches"] == [
        "allowed_neighbor.txt-1-line one",
        "allowed_neighbor.txt:2:NEEDLE_BOUNDARY here",
        "allowed_neighbor.txt-3-line three",
    ]


def test_workspace_search_default_context_lines_is_contract_stable(
    forge_env: ForgeEnvironment,
) -> None:
    service = forge_env.service
    workspace_id = service.workspace_create("demo", "context stability")["workspace_id"]

    default_call = service.workspace_search(workspace_id, "Repository")
    explicit_zero = service.workspace_search(workspace_id, "Repository", context_lines=0)
    assert explicit_zero == default_call
    assert default_call["matches"] == ["README.md:3:Repository instructions."]


def test_stale_locks_and_verification_invalidation(forge_env: ForgeEnvironment) -> None:
    service = forge_env.service
    workspace_id = service.workspace_create("demo", "stale lock")["workspace_id"]
    hello = service.workspace_read_file(workspace_id, "hello.txt")
    service.workspace_write_file(workspace_id, "hello.txt", "changed once\n", hello["sha256"])
    with pytest.raises(WorkspaceError, match="changed since"):
        service.workspace_write_file(workspace_id, "hello.txt", "changed twice\n", hello["sha256"])

    service.workspace_run_profile(workspace_id)
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


def test_commit_failure_reports_stage_and_invalidates_mutated_verified_tree(
    forge_env: ForgeEnvironment, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = forge_env.service
    workspace_id = service.workspace_create("demo", "commit hook failure")["workspace_id"]
    current = service.workspace_read_file(workspace_id, "hello.txt")
    service.workspace_write_file(
        workspace_id, "hello.txt", "changed for hook failure\n", current["sha256"]
    )
    service.workspace_run_profile(workspace_id)
    context = service.application.context

    def failed_commit(path: Path, message: str) -> tuple[str, str]:
        del message
        (path / "hook-mutated.txt").write_text("formatted by hook\n", encoding="utf-8")
        raise CommandError(
            "pre-commit hook failed",
            details={"commit_stage": "git_commit", "exit_code": 1},
        )

    monkeypatch.setattr(context.git, "commit", failed_commit)
    with pytest.raises(CommandError) as excinfo:
        service.workspace_commit(workspace_id, "Trigger hook failure")

    error = excinfo.value
    assert error.details["commit_stage"] == "git_commit"
    assert error.details["verification_invalidated"] is True
    assert "hook-mutated.txt" in error.details["changed_paths_after_failure"]
    assert context.store.load(workspace_id).last_verification is None


def test_change_budget_blocks_verification_and_commit(tmp_path: Path) -> None:
    env = create_forge_environment(tmp_path, max_changed_files=1, require_verification=False)
    service = env.service
    workspace_id = service.workspace_create("demo", "too broad")["workspace_id"]
    hello = service.workspace_read_file(workspace_id, "hello.txt")
    service.workspace_write_file(workspace_id, "hello.txt", "changed budget\n", hello["sha256"])
    service.workspace_write_file(workspace_id, "one.txt", "one\n", "<new>")
    with pytest.raises(WorkspaceError, match="Change budget exceeded"):
        service.workspace_run_profile(workspace_id)
    with pytest.raises(WorkspaceError, match="Change budget exceeded"):
        service.workspace_commit(workspace_id, "Too broad")


def test_repo_list_produces_exactly_one_bounded_audit_event(forge_env: ForgeEnvironment) -> None:
    listed = forge_env.service.repo_list()
    assert len(listed["repositories"]) == 1

    events = _audit_events(forge_env.root, "repo_list")
    assert len(events) == 1
    event = events[0]
    assert event["success"] is True
    assert event["details"]["repo_count"] == 1
    # Bounded: only a count plus the standard correlation/duration fields, never the
    # full repository listing (paths, profiles, diagnostics) that the result contains.
    assert set(event["details"]) == {
        "repo_count",
        "correlation_id",
        "duration_ms",
        "result_bytes",
        "is_mutating",
    }


def test_workspace_list_produces_exactly_one_bounded_audit_event(
    forge_env: ForgeEnvironment,
) -> None:
    forge_env.service.workspace_create("demo", "audit coverage for list")
    listed = forge_env.service.workspace_list()
    assert len(listed["workspaces"]) == 1

    events = _audit_events(forge_env.root, "workspace_list")
    assert len(events) == 1
    event = events[0]
    assert event["success"] is True
    assert event["details"]["workspace_count"] == 1
    assert set(event["details"]) == {
        "workspace_count",
        "correlation_id",
        "duration_ms",
        "result_bytes",
        "is_mutating",
    }


def test_workspace_list_audits_failure_without_leaking_internal_error_state(
    forge_env: ForgeEnvironment,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_list() -> list[object]:
        raise OSError("simulated registry directory read failure: /secret/state/path")

    monkeypatch.setattr(forge_env.service.state, "list", fail_list)
    with pytest.raises(OSError):
        forge_env.service.workspace_list()

    events = _audit_events(forge_env.root, "workspace_list")
    assert len(events) == 1
    event = events[0]
    assert event["success"] is False
    assert "error_code" in event["details"]
    assert "workspace_count" not in event["details"]
    assert "/secret/state/path" not in json.dumps(event["details"])
