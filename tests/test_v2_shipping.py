from __future__ import annotations

import json
from typing import Any

import pytest
from conftest import ForgeEnvironment
from pydantic import ValidationError

from repoforge.contracts.registry import V2_TOOL_SPECS
from repoforge.domain.errors import CommandError, ErrorCode, RepoForgeError
from repoforge.interfaces.mcp.server import SERVER_INSTRUCTIONS


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


def _prepare_issue_linked_pr_workspace(env: ForgeEnvironment) -> tuple[str, str]:
    created = env.service.workspace_create(
        "demo",
        "issue completion intent",
        issue_ids=("180", "181"),
    )
    workspace_id = created["workspace_id"]
    current = env.service.workspace_read_file(workspace_id, "hello.txt")
    env.service.workspace_write_file(
        workspace_id,
        "hello.txt",
        "changed for issue completion intent\n",
        current["sha256"],
    )
    env.service.workspace_run_profile(workspace_id, "full")
    committed = env.service.workspace_commit(workspace_id, "feat: bind issue completion intent")
    env.service.workspace_push(workspace_id, "shipping-issue-intent-push-0001")
    return workspace_id, "commit:" + committed["head_sha"]


def test_server_instructions_route_governed_issue_graph_workflows() -> None:
    assert "repo_issue mode=manage" in SERVER_INSTRUCTIONS
    assert "ticket_workflow" in SERVER_INSTRUCTIONS
    assert "operation get/wait" in SERVER_INSTRUCTIONS
    assert "raw GitHub mutation" in SERVER_INSTRUCTIONS
    assert "blind-retry" in SERVER_INSTRUCTIONS


def test_shipping_contracts_publish_action_specific_validation() -> None:
    commit = V2_TOOL_SPECS["workspace_commit"]
    push = V2_TOOL_SPECS["workspace_push"]
    pr = V2_TOOL_SPECS["workspace_pr"]
    evidence = V2_TOOL_SPECS["workspace_pr_evidence"]

    assert "expected_head_sha" in commit.input_model.model_fields
    assert "expected_fingerprint" in commit.input_model.model_fields
    assert "expected_remote_head" in push.input_model.model_fields
    assert "comment" in pr.input_model.model_json_schema()["$defs"]["WorkspacePrAction"]["enum"]
    assert "reconcile" in pr.input_model.model_json_schema()["$defs"]["WorkspacePrAction"]["enum"]
    assert "expected_remote_version" in pr.input_model.model_fields
    assert "review_comment_id" in pr.input_model.model_fields
    assert "issue_dispositions" in pr.input_model.model_fields

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


def test_workspace_pr_requires_complete_issue_dispositions_and_preserves_managed_intent(
    forge_env: ForgeEnvironment,
) -> None:
    workspace_id, evidence_ref = _prepare_issue_linked_pr_workspace(forge_env)

    with pytest.raises(RepoForgeError, match="explicit disposition") as incomplete:
        forge_env.service.workspace_pr(
            workspace_id,
            action="create_draft",
            title="Issue completion intent",
            body="Implement the linked tickets.",
            issue_dispositions=(
                {
                    "issue_number": 180,
                    "disposition": "closes",
                    "acceptance_evidence_ref": evidence_ref,
                },
            ),
            idempotency_key="shipping-issue-intent-create-incomplete",
        )
    assert incomplete.value.details["missing_issue_numbers"] == [181]

    created = forge_env.service.workspace_pr(
        workspace_id,
        action="create_draft",
        title="Issue completion intent",
        body="Implement the linked tickets.",
        issue_dispositions=(
            {
                "issue_number": 180,
                "disposition": "closes",
                "acceptance_evidence_ref": evidence_ref,
            },
            {
                "issue_number": 181,
                "disposition": "advances",
                "acceptance_evidence_ref": evidence_ref,
            },
        ),
        idempotency_key="shipping-issue-intent-create-0001",
    )
    V2_TOOL_SPECS["workspace_pr"].validate_output(created)
    assert created["issue_completion"]["intent_complete"] is True
    assert created["issue_completion"]["closes"] == [180]
    assert created["issue_completion"]["advances"] == [181]

    state = json.loads(forge_env.gh_state.read_text(encoding="utf-8"))
    branch = forge_env.service.workspace_status(workspace_id)["branch"]
    body = state["prs"][branch]["body"]
    assert "<!-- repoforge-pr-issue-dispositions:v1 -->" in body
    assert "Closes #180" in body
    assert "Advances #181" in body
    assert "Closes #181" not in body

    updated = forge_env.service.workspace_pr(
        workspace_id,
        action="update",
        body="Updated implementation summary.",
        idempotency_key="shipping-issue-intent-update-0001",
        expected_remote_version=created["remote_version"],
    )
    assert updated["issue_completion"] == created["issue_completion"]
    state = json.loads(forge_env.gh_state.read_text(encoding="utf-8"))
    updated_body = state["prs"][branch]["body"]
    assert updated_body.startswith("Updated implementation summary.")
    assert "Closes #180" in updated_body
    assert "Advances #181" in updated_body


def test_workspace_pr_create_reconciles_existing_pr_after_lost_create_response(
    forge_env: ForgeEnvironment,
) -> None:
    workspace_id, evidence_ref = _prepare_issue_linked_pr_workspace(forge_env)
    branch = forge_env.service.workspace_status(workspace_id)["branch"]
    state: dict[str, Any] = {}
    state.setdefault("prs", {})[branch] = {
        "number": 42,
        "title": "Incomplete recovered PR",
        "body": "PR created before the caller lost the response.",
        "url": "https://github.com/owner/demo/pull/42",
        "state": "OPEN",
        "isDraft": True,
        "mergeable": "MERGEABLE",
        "reviewDecision": "",
        "statusCheckRollup": [],
        "comments": [],
        "reviews": [],
        "updatedAt": "2026-07-21T14:00:00Z",
        "headRefOid": forge_env.service.workspace_status(workspace_id)["head_sha"],
    }
    forge_env.gh_state.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")

    recovered = forge_env.service.workspace_pr(
        workspace_id,
        action="create_draft",
        title="Recovered issue completion intent",
        body="Reconcile the authoritative existing PR.",
        issue_dispositions=(
            {
                "issue_number": 180,
                "disposition": "closes",
                "acceptance_evidence_ref": evidence_ref,
            },
            {
                "issue_number": 181,
                "disposition": "advances",
                "acceptance_evidence_ref": evidence_ref,
            },
        ),
        idempotency_key="shipping-issue-intent-recover-create-0001",
    )

    assert recovered["pull_request"]["number"] == 42
    state = json.loads(forge_env.gh_state.read_text(encoding="utf-8"))
    pr = state["prs"][branch]
    assert pr["title"] == "Recovered issue completion intent"
    assert "Reconcile the authoritative existing PR." in pr["body"]
    assert "Closes #180" in pr["body"]
    assert "Advances #181" in pr["body"]


def test_workspace_pr_reconciles_merged_completion_intent_without_closing_advances(
    forge_env: ForgeEnvironment,
) -> None:
    workspace_id, evidence_ref = _prepare_issue_linked_pr_workspace(forge_env)
    forge_env.service.workspace_pr(
        workspace_id,
        action="create_draft",
        title="Merged issue completion intent",
        body="Implement and advance the linked tickets.",
        issue_dispositions=(
            {
                "issue_number": 180,
                "disposition": "closes",
                "acceptance_evidence_ref": evidence_ref,
            },
            {
                "issue_number": 181,
                "disposition": "advances",
                "acceptance_evidence_ref": evidence_ref,
            },
        ),
        idempotency_key="shipping-issue-reconcile-create-0001",
    )
    state = json.loads(forge_env.gh_state.read_text(encoding="utf-8"))
    branch = forge_env.service.workspace_status(workspace_id)["branch"]
    state["prs"][branch].update(
        {
            "state": "MERGED",
            "baseRefName": "main",
            "mergedAt": "2026-07-23T15:00:00Z",
            "updatedAt": "2026-07-23T15:00:00Z",
        }
    )
    state["issues"] = {
        "180": {"state": "CLOSED"},
        "181": {"state": "OPEN"},
    }
    forge_env.gh_state.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    evidence = forge_env.service.workspace_pr_evidence(workspace_id)

    reconciled = forge_env.service.workspace_pr(
        workspace_id,
        action="reconcile",
        expected_remote_version=evidence["remote_version"],
    )
    V2_TOOL_SPECS["workspace_pr"].validate_output(reconciled)
    assert reconciled["reconciliation"] == {
        "merge_status": "merged",
        "closed_correctly": [180],
        "implemented_still_open": [],
        "intentionally_advanced": [181],
        "superseded": [],
        "acceptance_review_required": [],
        "closure_results": [],
    }
    final_state = json.loads(forge_env.gh_state.read_text(encoding="utf-8"))
    assert final_state["issues"]["181"]["state"] == "OPEN"


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
    replayed_update = forge_env.service.workspace_pr(
        workspace_id,
        action="update",
        title="Updated V2 shipping fixture",
        idempotency_key="shipping-pr-update-0001",
        expected_remote_version=remote_version,
    )
    assert replayed_update["remote_version"] == updated["remote_version"]

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
        expected_remote_version=replayed["remote_version"],
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


def test_workspace_pr_watch_returns_version_bound_durable_cursor(
    forge_env: ForgeEnvironment,
) -> None:
    workspace_id, created = _prepare_pr(forge_env)
    watched = forge_env.service.workspace_pr(
        workspace_id,
        action="watch",
        expected_remote_version=created["remote_version"],
        until="all_completed",
        timeout_seconds=30,
    )
    V2_TOOL_SPECS["workspace_pr"].validate_output(watched)
    assert watched["operation"]["kind"] == "pr_check_watch"
    assert watched["event_cursor"].startswith("pr-watch:")
    assert watched["remote_version"] == created["remote_version"]
    assert watched["terminal_reason"] is None

    state = json.loads(forge_env.gh_state.read_text(encoding="utf-8"))
    branch = forge_env.service.workspace_status(workspace_id)["branch"]
    state["prs"][branch]["title"] = "Concurrent metadata drift"
    forge_env.gh_state.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    with pytest.raises(RepoForgeError) as stale:
        forge_env.service.workspace_pr(
            workspace_id,
            action="watch",
            event_cursor=watched["event_cursor"],
        )
    assert stale.value.code is ErrorCode.PR_CHECK_WATCH_STALE


def test_workspace_pr_watch_rejects_stale_version_before_creation(
    forge_env: ForgeEnvironment,
) -> None:
    workspace_id, created = _prepare_pr(forge_env)
    state = json.loads(forge_env.gh_state.read_text(encoding="utf-8"))
    branch = forge_env.service.workspace_status(workspace_id)["branch"]
    state["prs"][branch]["title"] = "Concurrent watch metadata"
    forge_env.gh_state.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")

    with pytest.raises(RepoForgeError) as stale:
        forge_env.service.workspace_pr(
            workspace_id,
            action="watch",
            expected_remote_version=created["remote_version"],
            until="all_completed",
            timeout_seconds=30,
        )

    assert stale.value.code is ErrorCode.PR_REMOTE_VERSION_STALE
    assert stale.value.details["current_remote_version"] != created["remote_version"]
    assert stale.value.details["recovery_action"] == "reread_pr_overview"
    assert "current_title=Concurrent watch metadata" in stale.value.details["remote_delta"]


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


def test_workspace_pr_evidence_remote_version_round_trips_to_update(
    forge_env: ForgeEnvironment,
) -> None:
    workspace_id, created = _prepare_pr(forge_env)

    evidence = forge_env.service.workspace_pr_evidence(workspace_id)

    assert evidence["remote_version"] == created["remote_version"]
    assert evidence["remote_version"].startswith("prv2:")
    updated = forge_env.service.workspace_pr(
        workspace_id,
        action="update",
        title="Updated from evidence token",
        idempotency_key="shipping-pr-evidence-update-0001",
        expected_remote_version=evidence["remote_version"],
    )
    assert updated["pull_request"]["title"] == "Updated from evidence token"


def test_workspace_pr_stale_version_returns_typed_current_token(
    forge_env: ForgeEnvironment,
) -> None:
    workspace_id, _created = _prepare_pr(forge_env)
    reviewed = forge_env.service.workspace_pr_evidence(workspace_id)
    state = json.loads(forge_env.gh_state.read_text(encoding="utf-8"))
    branch = forge_env.service.workspace_status(workspace_id)["branch"]
    state["prs"][branch]["title"] = "Concurrent remote title"
    forge_env.gh_state.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")

    with pytest.raises(RepoForgeError) as stale:
        forge_env.service.workspace_pr(
            workspace_id,
            action="update",
            title="Must not overwrite concurrent state",
            idempotency_key="shipping-pr-stale-update-0001",
            expected_remote_version=reviewed["remote_version"],
        )

    current = forge_env.service.workspace_pr_evidence(workspace_id)
    assert stale.value.code is ErrorCode.PR_REMOTE_VERSION_STALE
    assert stale.value.retryable is False
    assert stale.value.details["field"] == "expected_remote_version"
    assert stale.value.details["expected"] == reviewed["remote_version"]
    assert stale.value.details["actual"] == current["remote_version"]
    assert stale.value.details["current_remote_version"] == current["remote_version"]
    assert stale.value.details["current_head_sha"] == current["pull_request"]["head_sha"]
    assert stale.value.details["current_updated_at"] == "2026-07-21T14:00:00Z"
    assert stale.value.details["recovery_action"] == "reread_pr_overview"
    assert "current_title=Concurrent remote title" in stale.value.details["remote_delta"]
    assert stale.value.details["result_reference"] == "workspace_pr_evidence:overview"
    assert "workspace_pr_evidence" in str(stale.value.safe_next_action)
    assert current["pull_request"]["title"] == "Concurrent remote title"


def test_workspace_pr_stale_recovery_redacts_remote_metadata(
    forge_env: ForgeEnvironment,
) -> None:
    workspace_id, _created = _prepare_pr(forge_env)
    reviewed = forge_env.service.workspace_pr_evidence(workspace_id)
    state = json.loads(forge_env.gh_state.read_text(encoding="utf-8"))
    branch = forge_env.service.workspace_status(workspace_id)["branch"]
    state["prs"][branch]["title"] = "Concurrent token=super-secret"
    forge_env.gh_state.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")

    with pytest.raises(RepoForgeError) as stale:
        forge_env.service.workspace_pr(
            workspace_id,
            action="update",
            title="Reviewed title",
            idempotency_key="shipping-pr-redacted-drift-0001",
            expected_remote_version=reviewed["remote_version"],
        )

    rendered = "\n".join(stale.value.details["remote_delta"])
    assert "super-secret" not in rendered
    assert "<redacted" in rendered


def test_workspace_pr_remote_version_tracks_comments_and_checks(
    forge_env: ForgeEnvironment,
) -> None:
    workspace_id, _created = _prepare_pr(forge_env)
    initial = forge_env.service.workspace_pr_evidence(workspace_id)
    state = json.loads(forge_env.gh_state.read_text(encoding="utf-8"))
    branch = forge_env.service.workspace_status(workspace_id)["branch"]
    state["prs"][branch]["comments"] = [{"id": 9001, "updatedAt": "2026-07-21T15:01:00Z"}]
    state["prs"][branch]["statusCheckRollup"] = [
        {"name": "unit", "status": "COMPLETED", "conclusion": "SUCCESS"}
    ]
    forge_env.gh_state.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")

    changed = forge_env.service.workspace_pr_evidence(workspace_id)

    assert changed["remote_version"].startswith("prv2:")
    assert changed["remote_version"] != initial["remote_version"]


def test_workspace_pr_conversation_drift_rejects_stale_write(
    forge_env: ForgeEnvironment,
) -> None:
    workspace_id, _created = _prepare_pr(forge_env)
    reviewed = forge_env.service.workspace_pr_evidence(workspace_id)
    state = json.loads(forge_env.gh_state.read_text(encoding="utf-8"))
    branch = forge_env.service.workspace_status(workspace_id)["branch"]
    state["prs"][branch]["comments"] = [{"id": 9001, "updatedAt": "2026-07-21T15:01:00Z"}]
    forge_env.gh_state.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")

    with pytest.raises(RepoForgeError) as stale:
        forge_env.service.workspace_pr(
            workspace_id,
            action="update",
            title="Must not overwrite a new conversation",
            idempotency_key="shipping-pr-comment-drift-0001",
            expected_remote_version=reviewed["remote_version"],
        )

    assert stale.value.code is ErrorCode.PR_REMOTE_VERSION_STALE
    assert "comments=1" in stale.value.details["remote_delta"]
    assert stale.value.details["current_remote_version"] != reviewed["remote_version"]


def test_workspace_pr_head_drift_rejects_stale_write(
    forge_env: ForgeEnvironment,
) -> None:
    workspace_id, _created = _prepare_pr(forge_env)
    reviewed = forge_env.service.workspace_pr_evidence(workspace_id)
    state = json.loads(forge_env.gh_state.read_text(encoding="utf-8"))
    branch = forge_env.service.workspace_status(workspace_id)["branch"]
    state["prs"][branch]["headRefOid"] = "f" * 40
    forge_env.gh_state.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")

    with pytest.raises(RepoForgeError) as stale:
        forge_env.service.workspace_pr(
            workspace_id,
            action="update",
            title="Must not overwrite a new head",
            idempotency_key="shipping-pr-head-drift-0001",
            expected_remote_version=reviewed["remote_version"],
        )

    assert stale.value.code is ErrorCode.PR_REMOTE_VERSION_STALE
    assert stale.value.details["current_head_sha"] == "f" * 40
    assert stale.value.details["current_remote_version"] != reviewed["remote_version"]


def test_workspace_pr_remote_version_refuses_incomplete_provider_coverage(
    forge_env: ForgeEnvironment,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id, _created = _prepare_pr(forge_env)
    context = forge_env.service.application.context
    original = context.github.status

    def incomplete_status(path: Any, branch: str) -> dict[str, Any]:
        payload = dict(original(path, branch))
        payload.pop("updatedAt", None)
        return payload

    monkeypatch.setattr(context.github, "status", incomplete_status)
    with pytest.raises(RepoForgeError) as incomplete:
        forge_env.service.workspace_pr_evidence(workspace_id)

    assert incomplete.value.code is ErrorCode.PR_REMOTE_VERSION_INCOMPLETE
    assert incomplete.value.retryable is False
    assert "updatedAt" in incomplete.value.details["missing_coverage"]


def test_workspace_pr_remote_version_refuses_truncated_provider_collections(
    forge_env: ForgeEnvironment,
) -> None:
    workspace_id, _created = _prepare_pr(forge_env)
    state = json.loads(forge_env.gh_state.read_text(encoding="utf-8"))
    branch = forge_env.service.workspace_status(workspace_id)["branch"]
    state["prs"][branch]["comments"] = [
        {"id": index, "updatedAt": "2026-07-21T15:01:00Z"} for index in range(101)
    ]
    forge_env.gh_state.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")

    with pytest.raises(RepoForgeError) as incomplete:
        forge_env.service.workspace_pr_evidence(workspace_id)

    assert incomplete.value.code is ErrorCode.PR_REMOTE_VERSION_INCOMPLETE
    assert "comments:truncated" in incomplete.value.details["missing_coverage"]
