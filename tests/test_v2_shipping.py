from __future__ import annotations

import json
from typing import Any

import pytest
from conftest import ForgeEnvironment
from pydantic import ValidationError

from repoforge.contracts.registry import V2_TOOL_SPECS
from repoforge.domain.errors import CommandError, ErrorCode, RepoForgeError


def _prepare_commit(env: ForgeEnvironment) -> tuple[str, dict[str, Any]]:
    created = env.service.workspace_create("demo", "v2 shipping")
    workspace_id = created["workspace_id"]
    current = env.service.workspace_read_file(workspace_id, "hello.txt")
    env.service.workspace_write_file(
        workspace_id,
        "hello.txt",
        "changed for v2 shipping\n",
        current["sha256"],
    )
    verified = env.service.workspace_run_profile(workspace_id, "full")
    return workspace_id, verified


def _prepare_pr(env: ForgeEnvironment) -> tuple[str, dict[str, Any]]:
    workspace_id, _ = _prepare_commit(env)
    env.service.workspace_commit(workspace_id, "feat: prepare shipping fixture")
    env.service.workspace_push(workspace_id, "shipping-push-0001")
    created = env.service.workspace_pr(
        workspace_id,
        action="create_draft",
        title="V2 shipping fixture",
        body="Exercise consolidated shipping tools.",
        idempotency_key="shipping-pr-create-0001",
    )
    return workspace_id, created


def test_shipping_contracts_publish_action_specific_validation() -> None:
    commit = V2_TOOL_SPECS["workspace_commit"]
    push = V2_TOOL_SPECS["workspace_push"]
    pr = V2_TOOL_SPECS["workspace_pr"]
    evidence = V2_TOOL_SPECS["workspace_pr_evidence"]

    assert "expected_head_sha" in commit.input_model.model_fields
    assert "expected_fingerprint" in commit.input_model.model_fields
    assert "expected_remote_head" in push.input_model.model_fields
    assert "comment" in pr.input_model.model_json_schema()["$defs"]["WorkspacePrAction"]["enum"]
    assert "expected_remote_version" in pr.input_model.model_fields
    assert "review_comment_id" in pr.input_model.model_fields

    with pytest.raises(ValidationError):
        pr.validate_input({"workspace_id": "ws", "action": "create_draft"})
    with pytest.raises(ValidationError):
        pr.validate_input({"workspace_id": "ws", "action": "comment", "body": "reply"})
    with pytest.raises(ValidationError):
        pr.validate_input(
            {
                "workspace_id": "ws",
                "action": "update",
                "title": "stale blind update",
                "idempotency_key": "shipping-update-no-version",
            }
        )
    with pytest.raises(ValidationError):
        evidence.validate_input({"workspace_id": "ws", "detail": "failure"})


def test_workspace_commit_and_push_return_typed_exact_state_evidence(
    forge_env: ForgeEnvironment,
) -> None:
    workspace_id, verified = _prepare_commit(forge_env)
    status = forge_env.service.workspace_status(workspace_id)

    committed = forge_env.service.workspace_commit(
        workspace_id,
        "feat: typed shipping commit",
        expected_head_sha=status["head_sha"],
        expected_fingerprint=status["workspace_fingerprint"],
    )
    V2_TOOL_SPECS["workspace_commit"].validate_output(committed)
    assert committed["previous_head_sha"] == status["head_sha"]
    assert committed["head_sha"] != committed["previous_head_sha"]
    assert committed["verification_fingerprint"] == verified["fingerprint"]
    assert committed["committed"] is True

    pushed = forge_env.service.workspace_push(
        workspace_id,
        idempotency_key="shipping-push-typed-0001",
        expected_remote_head=None,
    )
    V2_TOOL_SPECS["workspace_push"].validate_output(pushed)
    assert pushed["remote_head_before"] is None
    assert pushed["remote_head_after"] == committed["head_sha"]
    assert pushed["pushed"] is True
    assert pushed["retryable_rejection"] is False

    replay = forge_env.service.workspace_push(
        workspace_id,
        idempotency_key="shipping-push-typed-0002",
        expected_remote_head=committed["head_sha"],
    )
    assert replay["pushed"] is False
    assert replay["remote_head_before"] == committed["head_sha"]


def test_workspace_commit_rejects_stale_exact_state(forge_env: ForgeEnvironment) -> None:
    workspace_id, _ = _prepare_commit(forge_env)
    with pytest.raises(RepoForgeError):
        forge_env.service.workspace_commit(
            workspace_id,
            "feat: stale commit",
            expected_head_sha="a" * 40,
            expected_fingerprint="b" * 64,
        )


def test_workspace_pr_create_update_comment_and_comment_replay(
    forge_env: ForgeEnvironment,
) -> None:
    workspace_id, created = _prepare_pr(forge_env)
    V2_TOOL_SPECS["workspace_pr"].validate_output(created)
    assert created["pull_request"]["draft"] is True
    remote_version = created["remote_version"]

    updated = forge_env.service.workspace_pr(
        workspace_id,
        action="update",
        title="Updated V2 shipping fixture",
        idempotency_key="shipping-pr-update-0001",
        expected_remote_version=remote_version,
    )
    assert updated["pull_request"]["title"] == "Updated V2 shipping fixture"
    assert updated["remote_version"] != remote_version

    commented = forge_env.service.workspace_pr(
        workspace_id,
        action="comment",
        body="Addressed the review feedback.",
        evidence_ref="commit:" + forge_env.service.workspace_status(workspace_id)["head_sha"],
        idempotency_key="shipping-pr-comment-0001",
        expected_remote_version=updated["remote_version"],
    )
    replayed = forge_env.service.workspace_pr(
        workspace_id,
        action="comment",
        body="Addressed the review feedback.",
        evidence_ref="commit:" + forge_env.service.workspace_status(workspace_id)["head_sha"],
        idempotency_key="shipping-pr-comment-0001",
        expected_remote_version=updated["remote_version"],
    )
    assert commented["comment"]["result"] == "created"
    assert replayed["comment"]["idempotent_replay"] is True
    replied = forge_env.service.workspace_pr(
        workspace_id,
        action="comment",
        body="Replying to one review thread.",
        evidence_ref="commit:" + forge_env.service.workspace_status(workspace_id)["head_sha"],
        review_comment_id=777,
        idempotency_key="shipping-pr-review-reply-0001",
        expected_remote_version=updated["remote_version"],
    )
    assert replied["comment"]["review_comment_id"] == 777
    state = json.loads(forge_env.gh_state.read_text(encoding="utf-8"))
    assert len(state["pr_comments"]) == 1
    assert len(state["pr_review_comments"]) == 1


def test_workspace_pr_comment_reconciles_ambiguous_failure_after_remote_effect(
    forge_env: ForgeEnvironment,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id, created = _prepare_pr(forge_env)
    context = forge_env.service.application.context
    original = context.github.pr_comment
    failed = False

    def fail_after_effect(path: Any, pr_number: int, body: str):
        nonlocal failed
        result = original(path, pr_number, body)
        if not failed:
            failed = True
            raise CommandError("GitHub returned 502 after accepting the comment")
        return result

    monkeypatch.setattr(context.github, "pr_comment", fail_after_effect)
    request = {
        "workspace_id": workspace_id,
        "action": "comment",
        "body": "The remote may have accepted this comment.",
        "evidence_ref": "commit:" + forge_env.service.workspace_status(workspace_id)["head_sha"],
        "idempotency_key": "shipping-pr-comment-ambiguous-0001",
        "expected_remote_version": created["remote_version"],
    }
    with pytest.raises(RepoForgeError) as unknown:
        forge_env.service.workspace_pr(**request)
    assert unknown.value.code is ErrorCode.EFFECT_OUTCOME_UNKNOWN
    assert unknown.value.retryable is False
    assert unknown.value.details["effect_boundary_crossed"] is True
    assert str(unknown.value.details["operation_id"]).startswith("op-")
    assert str(unknown.value.details["receipt_id"]).startswith("receipt-")

    reconciled = forge_env.service.workspace_pr(**request)
    assert reconciled["comment"]["result"] == "reconciled"
    assert reconciled["comment"]["idempotent_replay"] is True
    state = json.loads(forge_env.gh_state.read_text(encoding="utf-8"))
    assert len(state["pr_comments"]) == 1


def test_workspace_pr_watch_returns_durable_cursor(
    forge_env: ForgeEnvironment,
) -> None:
    workspace_id, _ = _prepare_pr(forge_env)
    watched = forge_env.service.workspace_pr(
        workspace_id,
        action="watch",
        until="all_completed",
        timeout_seconds=30,
    )
    V2_TOOL_SPECS["workspace_pr"].validate_output(watched)
    assert watched["operation"]["kind"] == "pr_check_watch"
    assert watched["event_cursor"].startswith("pr-watch:")
    assert watched["terminal_reason"] is None


def test_workspace_pr_evidence_supports_delta_and_detail_zoom(
    forge_env: ForgeEnvironment,
) -> None:
    workspace_id, _ = _prepare_pr(forge_env)
    state = json.loads(forge_env.gh_state.read_text(encoding="utf-8"))
    head = forge_env.service.workspace_status(workspace_id)["head_sha"]
    state["checks"] = [
        {
            "name": "unit",
            "state": "FAILURE",
            "bucket": "fail",
            "required": True,
            "link": "https://github.com/owner/demo/actions/runs/1001/job/101",
            "workflow": "CI",
            "description": "failed",
            "startedAt": "",
            "completedAt": "",
            "head_sha": head,
        }
    ]
    state["check_runs"] = {
        "101": {
            "id": 101,
            "name": "unit",
            "head_sha": head,
            "status": "completed",
            "conclusion": "failure",
            "details_url": "https://github.com/owner/demo/actions/runs/1001/job/101",
            "html_url": "https://github.com/owner/demo/actions/runs/1001/job/101",
            "started_at": "",
            "completed_at": "",
            "output": {
                "title": "Unit failed",
                "summary": "secret=hidden",
                "text": "trace",
                "annotations_count": 1,
            },
            "app": {"name": "GitHub Actions"},
            "job_id": 101,
        }
    }
    state["annotations"] = {
        "101": [
            {
                "path": "hello.txt",
                "start_line": 1,
                "end_line": 1,
                "annotation_level": "failure",
                "title": "Assertion",
                "message": "expected changed",
                "raw_details": "token=super-secret",
            }
        ]
    }
    forge_env.gh_state.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")

    overview = forge_env.service.workspace_pr_evidence(workspace_id)
    V2_TOOL_SPECS["workspace_pr_evidence"].validate_output(overview)
    assert overview["changed_since"] is True
    assert overview["checks"][0]["status"] == "fail"

    unchanged = forge_env.service.workspace_pr_evidence(
        workspace_id,
        since=overview["delta_token"],
    )
    assert unchanged["changed_since"] is False
    assert unchanged["checks"] == []

    detail = forge_env.service.workspace_pr_evidence(
        workspace_id,
        detail="check",
        check_selector="check-run:101",
    )
    assert detail["checks"][0]["annotations"]

    failure = forge_env.service.workspace_pr_evidence(
        workspace_id,
        detail="failure",
        check_selector="check-run:101",
        max_excerpt_lines=20,
    )
    assert failure["failure_excerpt"]
    assert all("super-secret" not in line for line in failure["failure_excerpt"])
