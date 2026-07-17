from __future__ import annotations

import json
import shutil
from dataclasses import replace
from pathlib import Path

import pytest
from conftest import ForgeEnvironment, create_forge_environment, git

from repoforge.application.workspace.edit import FileEdit, TextEdit
from repoforge.domain.approval import ApprovalStatus, decide_approval
from repoforge.domain.errors import (
    CommandError,
    ConfigError,
    ErrorCode,
    SecurityError,
    WorkspaceError,
)
from repoforge.domain.issue_writes import IssueWritePolicy
from repoforge.ports.issue_mutation import RemoteComment, RemoteIssue


class _FakeIssueMutationGateway:
    def __init__(self) -> None:
        self.issues: dict[int, RemoteIssue] = {
            7: RemoteIssue(7, 7007, "Existing issue", "open", "", "https://example/7"),
            8: RemoteIssue(8, 7008, "Target issue", "open", "", "https://example/8"),
        }
        self.comments: dict[int, list[RemoteComment]] = {}
        self.sub_issue_links: dict[int, set[int]] = {}
        self.blocked_by_links: dict[int, set[int]] = {}
        self.fail_comment_after_effect_once = False
        self.force_comment_scan_truncated = False
        self._next_comment = 1
        self._next_issue = 20

    def issue_details(self, cwd: Path, issue_number: int) -> RemoteIssue:
        del cwd
        return self.issues[issue_number]

    def issue_comments(
        self, cwd: Path, issue_number: int, *, max_comments: int
    ) -> tuple[tuple[RemoteComment, ...], bool]:
        del cwd
        values = self.comments.get(issue_number, [])
        return (
            tuple(values[:max_comments]),
            self.force_comment_scan_truncated or len(values) > max_comments,
        )

    def recent_issues(self, cwd: Path, *, max_issues: int) -> tuple[tuple[RemoteIssue, ...], bool]:
        del cwd
        values = sorted(self.issues.values(), key=lambda item: item.issue_number, reverse=True)
        return tuple(values[:max_issues]), len(values) > max_issues

    def issue_comment(self, cwd: Path, issue_number: int, body: str) -> RemoteComment:
        del cwd
        comment = RemoteComment(
            self._next_comment,
            body,
            f"https://example/{issue_number}#comment-{self._next_comment}",
        )
        self._next_comment += 1
        self.comments.setdefault(issue_number, []).append(comment)
        if self.fail_comment_after_effect_once:
            self.fail_comment_after_effect_once = False
            raise CommandError("simulated lost GitHub response")
        return comment

    def set_issue_state(self, cwd: Path, issue_number: int, state: str) -> RemoteIssue:
        del cwd
        current = self.issues[issue_number]
        updated = replace(current, state=state)
        self.issues[issue_number] = updated
        return updated

    def create_issue(self, cwd: Path, title: str, body: str) -> RemoteIssue:
        del cwd
        number = self._next_issue
        self._next_issue += 1
        issue = RemoteIssue(number, number + 7000, title, "open", body, f"https://example/{number}")
        self.issues[number] = issue
        return issue

    def sub_issues(
        self, cwd: Path, issue_number: int, *, max_issues: int
    ) -> tuple[tuple[RemoteIssue, ...], bool]:
        del cwd
        values = [
            self.issues[number] for number in sorted(self.sub_issue_links.get(issue_number, set()))
        ]
        return tuple(values[:max_issues]), len(values) > max_issues

    def blocked_by(
        self, cwd: Path, issue_number: int, *, max_issues: int
    ) -> tuple[tuple[RemoteIssue, ...], bool]:
        del cwd
        values = [
            self.issues[number] for number in sorted(self.blocked_by_links.get(issue_number, set()))
        ]
        return tuple(values[:max_issues]), len(values) > max_issues

    def add_sub_issue(self, cwd: Path, issue_number: int, sub_issue_id: int) -> RemoteIssue:
        del cwd
        target = next(item for item in self.issues.values() if item.database_id == sub_issue_id)
        self.sub_issue_links.setdefault(issue_number, set()).add(target.issue_number)
        return target

    def add_blocked_by(self, cwd: Path, issue_number: int, blocker_issue_id: int) -> RemoteIssue:
        del cwd
        target = next(item for item in self.issues.values() if item.database_id == blocker_issue_id)
        self.blocked_by_links.setdefault(issue_number, set()).add(target.issue_number)
        return target


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


def test_v2_repo_list_history_and_pr_facades_are_compact_and_path_safe(
    forge_env: ForgeEnvironment,
) -> None:
    service = forge_env.service

    listed = service.repo_list_v2(limit=10)
    assert listed["repositories"] == [
        {
            "repo_id": "demo",
            "capabilities": ["read", "write", "publish", "verify"],
            "default_ref": "main",
        }
    ]
    assert str(forge_env.source) not in json.dumps(listed)

    log = service.repo_history_v2("demo", mode="log", limit=5)
    assert log["mode"] == "log"
    assert log["commits"][0]["subject"] == "initial"
    assert log["commit"] is None
    assert log["comparison"] is None

    commit = service.repo_history_v2("demo", mode="commit", ref="main")
    assert commit["commit"]["sha"] == log["commits"][0]["sha"]
    assert commit["commits"] == []

    comparison = service.repo_history_v2(
        "demo",
        mode="compare",
        base_ref="main",
        head_ref="main",
    )
    assert comparison["comparison"]["ahead"] == 0
    assert comparison["comparison"]["behind"] == 0
    assert comparison["comparison"]["files"] == []

    pr = service.repo_pr_read_v2("demo", 8, detail="overview")
    assert pr["pull_request"]["number"] == 8
    assert pr["pull_request"]["freshness"] == "live"
    assert str(forge_env.source) not in json.dumps(pr)


def test_v2_repo_history_cursor_continues_exact_log_page(
    forge_env: ForgeEnvironment,
) -> None:
    for index in range(3):
        path = forge_env.source / f"history-{index}.txt"
        path.write_text(f"{index}\n", encoding="utf-8")
        git("add", path.name, cwd=forge_env.source)
        git("commit", "-m", f"history {index}", cwd=forge_env.source)

    first = forge_env.service.repo_history_v2("demo", mode="log", limit=2)
    assert [item["subject"] for item in first["commits"]] == ["history 2", "history 1"]
    assert first["next_cursor"] is not None

    second = forge_env.service.repo_history_v2(
        "demo",
        mode="log",
        limit=2,
        cursor=first["next_cursor"],
    )
    assert [item["subject"] for item in second["commits"]] == ["history 0", "initial"]
    assert second["next_cursor"] is None


def test_v2_repo_issue_comment_replays_and_reconciles_lost_response(
    forge_env: ForgeEnvironment,
) -> None:
    service = forge_env.service
    gateway = _FakeIssueMutationGateway()
    object.__setattr__(service.application.context, "issue_mutations", gateway)

    first = service.repo_issue_v2(
        "demo",
        mode="comment",
        issue_number=7,
        body="Verification passed. token=secret-value",
        evidence_ref="commit:abc123",
        idempotency_key="repo-issue-comment-0001",
    )
    replay = service.repo_issue_v2(
        "demo",
        mode="comment",
        issue_number=7,
        body="Verification passed. token=secret-value",
        evidence_ref="commit:abc123",
        idempotency_key="repo-issue-comment-0001",
    )

    assert replay == first
    assert first["mutation"]["result"] == "applied"
    assert first["mutation"]["external_writes"] == 1
    assert len(gateway.comments[7]) == 1
    assert "secret-value" not in gateway.comments[7][0].body
    assert "<!-- repoforge-issue-write:" in gateway.comments[7][0].body

    gateway.fail_comment_after_effect_once = True
    with pytest.raises(ConfigError) as uncertain:
        service.repo_issue_v2(
            "demo",
            mode="comment",
            issue_number=7,
            body="Second verified result.",
            evidence_ref="verification:run-2",
            idempotency_key="repo-issue-comment-0002",
        )
    assert uncertain.value.code is ErrorCode.IDEMPOTENCY_UNCERTAIN
    reconciled = service.repo_issue_v2(
        "demo",
        mode="comment",
        issue_number=7,
        body="Second verified result.",
        evidence_ref="verification:run-2",
        idempotency_key="repo-issue-comment-0002",
    )

    assert reconciled["mutation"]["result"] == "reconciled"
    assert reconciled["mutation"]["external_writes"] == 0
    assert len(gateway.comments[7]) == 2


def test_v2_repo_issue_policy_approval_create_and_rate_gates(
    forge_env: ForgeEnvironment,
) -> None:
    service = forge_env.service
    gateway = _FakeIssueMutationGateway()
    ctx = service.application.context
    object.__setattr__(ctx, "issue_mutations", gateway)
    configured = service.config.repositories["demo"]
    policy = IssueWritePolicy(
        enabled_ops=("comment", "close", "create"),
        approval_required_ops=("close",),
        max_writes_per_call=2,
        max_writes_per_window=2,
        window_seconds=3600,
        create_title_prefix="[FOLLOWUP]",
    )
    config = replace(
        service.config,
        repositories={
            **service.config.repositories,
            "demo": replace(configured, issue_writes=policy),
        },
    )
    object.__setattr__(ctx, "config", config)
    service.config = config

    pending = service.repo_issue_v2(
        "demo",
        mode="close",
        issue_number=7,
        evidence_ref="verification:full-green",
        idempotency_key="repo-issue-close-0001",
    )
    approval_id = pending["mutation"]["approval_request_id"]
    assert pending["mutation"]["result"] == "pending_approval"
    assert gateway.issues[7].state == "open"

    approvals, _ = ctx.approval_stores()
    envelope = approvals.read(approval_id)
    assert envelope is not None
    approved = decide_approval(
        envelope.value,
        ApprovalStatus.ACCEPTED,
        actor="operator@example.com",
        decided_at="2026-07-17T10:00:00+00:00",
        reason="Reviewed exact issue mutation.",
    )
    approvals.save(approved, expected_revision=envelope.revision)
    closed = service.repo_issue_v2(
        "demo",
        mode="close",
        issue_number=7,
        evidence_ref="verification:full-green",
        idempotency_key="repo-issue-close-0001",
        approval_request_id=approval_id,
    )

    assert closed["mutation"]["result"] == "applied"
    assert closed["mutation"]["external_writes"] == 2
    assert gateway.issues[7].state == "closed"

    with pytest.raises(ConfigError, match="external mutation window limit"):
        service.repo_issue_v2(
            "demo",
            mode="create",
            title="Missing prefix is normalized",
            body="Follow-up work.",
            evidence_ref="issue:7",
            idempotency_key="repo-issue-create-0001",
        )


def test_v2_repo_issue_create_reopen_and_native_links_are_explicit(
    forge_env: ForgeEnvironment,
) -> None:
    service = forge_env.service
    gateway = _FakeIssueMutationGateway()
    ctx = service.application.context
    object.__setattr__(ctx, "issue_mutations", gateway)
    configured = service.config.repositories["demo"]
    policy = IssueWritePolicy(
        enabled_ops=("comment", "reopen", "link", "create"),
        max_writes_per_call=2,
        max_writes_per_window=20,
        create_title_prefix="[FOLLOWUP]",
    )
    config = replace(
        service.config,
        repositories={
            **service.config.repositories,
            "demo": replace(configured, issue_writes=policy),
        },
    )
    object.__setattr__(ctx, "config", config)
    service.config = config
    gateway.issues[7] = replace(gateway.issues[7], state="closed")

    created = service.repo_issue_v2(
        "demo",
        mode="create",
        title="Investigate regression",
        body="Reproduce and fix the regression.",
        evidence_ref="issue:7",
        idempotency_key="repo-issue-create-0002",
    )
    created_issue = gateway.issues[created["mutation"]["issue_number"]]
    assert created_issue.title == "[FOLLOWUP] Investigate regression"
    assert "## Objective" in created_issue.body
    assert "<!-- repoforge-issue-write:" in created_issue.body

    reopened = service.repo_issue_v2(
        "demo",
        mode="reopen",
        issue_number=7,
        evidence_ref="verification:reopened",
        idempotency_key="repo-issue-reopen-0001",
    )
    assert reopened["mutation"]["external_writes"] == 2
    assert gateway.issues[7].state == "open"

    sub_issue = service.repo_issue_v2(
        "demo",
        mode="link",
        issue_number=7,
        target_issue=8,
        link_type="sub_issue",
        evidence_ref="roadmap:7",
        idempotency_key="repo-issue-link-sub-0001",
    )
    blocked_by = service.repo_issue_v2(
        "demo",
        mode="link",
        issue_number=7,
        target_issue=8,
        link_type="blocked_by",
        evidence_ref="dependency:8",
        idempotency_key="repo-issue-link-block-0001",
    )
    superseded = service.repo_issue_v2(
        "demo",
        mode="link",
        issue_number=7,
        target_issue=8,
        link_type="supersede",
        evidence_ref="replacement:8",
        idempotency_key="repo-issue-link-super-0001",
    )

    assert sub_issue["mutation"]["link_type"] == "sub_issue"
    assert blocked_by["mutation"]["link_type"] == "blocked_by"
    assert superseded["mutation"]["link_type"] == "supersede"
    assert gateway.sub_issue_links[7] == {8}
    assert gateway.blocked_by_links[7] == {8}
    assert any(comment.body.startswith("Duplicate of #8") for comment in gateway.comments[7])
    assert gateway.issues[7].state == "open"


def test_v2_repo_issue_incomplete_reconciliation_fails_closed(
    forge_env: ForgeEnvironment,
) -> None:
    service = forge_env.service
    gateway = _FakeIssueMutationGateway()
    gateway.force_comment_scan_truncated = True
    object.__setattr__(service.application.context, "issue_mutations", gateway)

    with pytest.raises(ConfigError, match="reconciliation is incomplete"):
        service.repo_issue_v2(
            "demo",
            mode="comment",
            issue_number=7,
            body="Do not post blindly.",
            evidence_ref="verification:bounded-scan",
            idempotency_key="repo-issue-comment-incomplete",
        )
    assert gateway.comments == {}


def test_v2_repo_issue_disabled_operation_fails_before_remote_write(
    forge_env: ForgeEnvironment,
) -> None:
    service = forge_env.service
    gateway = _FakeIssueMutationGateway()
    object.__setattr__(service.application.context, "issue_mutations", gateway)

    with pytest.raises(ConfigError, match="not enabled"):
        service.repo_issue_v2(
            "demo",
            mode="close",
            issue_number=7,
            evidence_ref="verification:none",
            idempotency_key="repo-issue-close-disabled",
        )
    assert gateway.comments == {}
    assert gateway.issues[7].state == "open"


def test_v2_workspace_lifecycle_is_path_safe_filtered_and_sectioned(
    forge_env: ForgeEnvironment,
) -> None:
    service = forge_env.service
    first = service.workspace_create_v2(
        "demo",
        "v2 lifecycle first",
        idempotency_key="workspace-create-v2-first-0001",
        issue_ids=("188",),
    )
    second = service.workspace_create_v2(
        "demo",
        "v2 lifecycle second",
        idempotency_key="workspace-create-v2-second-0001",
    )

    assert "path" not in first
    assert first["workspace_fingerprint"]
    assert first["issue_ids"] == ["188"]
    assert str(forge_env.root) not in json.dumps(first)

    page_one = service.workspace_list_v2(limit=1)
    assert len(page_one["workspaces"]) == 1
    assert page_one["next_cursor"] is not None
    assert "path" not in page_one["workspaces"][0]
    page_two = service.workspace_list_v2(limit=1, cursor=page_one["next_cursor"])
    assert len(page_two["workspaces"]) == 1
    assert {
        page_one["workspaces"][0]["workspace_id"],
        page_two["workspaces"][0]["workspace_id"],
    } == {first["workspace_id"], second["workspace_id"]}
    assert str(forge_env.root) not in json.dumps(page_one)

    status = service.workspace_status_v2(
        first["workspace_id"],
        sections=("local", "base", "hygiene"),
    )
    assert [section["section"] for section in status["sections"]] == [
        "local",
        "base",
        "hygiene",
    ]
    assert status["fingerprint_source"] in {"cache", "scan"}
    assert status["workspace_fingerprint"] == first["workspace_fingerprint"]
    assert str(forge_env.root) not in json.dumps(status)

    bounded = service.workspace_status_v2(
        first["workspace_id"],
        sections=("local", "base", "hygiene"),
        byte_budget=250,
    )
    assert bounded["truncated"] is True

    removed = service.workspace_remove_v2(second["workspace_id"])
    assert removed["removed"] is True
    assert removed["remote_untouched"] is True
    assert "Remote branches" in removed["tombstone"]


def test_v2_workspace_list_surfaces_missing_worktree_cleanup_guidance(
    forge_env: ForgeEnvironment,
) -> None:
    service = forge_env.service
    created = service.workspace_create_v2("demo", "missing v2 worktree")
    record = service.state.load(created["workspace_id"])
    shutil.rmtree(record.path)

    missing = service.workspace_list_v2(exists=False)

    assert [item["workspace_id"] for item in missing["workspaces"]] == [created["workspace_id"]]
    assert missing["cleanup_guidance"]
    assert str(forge_env.root) not in json.dumps(missing)


def test_v2_workspace_format_changed_reports_changed_and_noop_evidence(
    forge_env: ForgeEnvironment,
) -> None:
    service = forge_env.service
    created = service.workspace_create_v2("demo", "v2 format changed")
    workspace_id = created["workspace_id"]
    current = service.workspace_read_file(workspace_id, "hello.txt")
    service.workspace_write_file(
        workspace_id,
        "hello.txt",
        "needs-format\n",
        current["sha256"],
    )
    dirty = service.workspace_status(workspace_id)

    changed = service.workspace_format_changed_v2(
        workspace_id,
        dirty["workspace_fingerprint"],
    )

    assert changed["changed"] is True
    assert changed["formatters"][0]["outcome"] == "changed"
    assert changed["formatters"][0]["changed_paths"] == ["hello.txt"]
    noop = service.workspace_format_changed_v2(
        workspace_id,
        changed["workspace_fingerprint"],
    )
    assert noop["changed"] is False
    assert noop["formatters"][0]["outcome"] == "no_op"


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
