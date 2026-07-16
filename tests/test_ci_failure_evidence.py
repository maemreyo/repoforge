from __future__ import annotations

import json

import pytest
from conftest import ForgeEnvironment

from repoforge.domain.ci_evidence import (
    classify_ci_failure,
    parse_check_selector,
    sanitize_ci_text,
)
from repoforge.domain.errors import ErrorCode, RepoForgeError


def _publish_workspace(env: ForgeEnvironment) -> tuple[str, str]:
    service = env.service
    workspace_id = service.workspace_create("demo", "CI evidence")["workspace_id"]
    hello = service.workspace_read_file(workspace_id, "hello.txt")
    service.workspace_write_file(
        workspace_id,
        "hello.txt",
        "changed for CI evidence\n",
        hello["sha256"],
    )
    service.workspace_run_profile(workspace_id)
    committed = service.workspace_commit(workspace_id, "Prepare CI evidence")
    service.workspace_push(workspace_id)
    service.workspace_create_draft_pr(workspace_id, "CI evidence", "Test CI evidence")
    return workspace_id, str(committed["head_sha"])


def _failed_state(head_sha: str) -> dict[str, object]:
    details_url = "https://github.com/owner/demo/actions/runs/9001/job/7001"
    return {
        "checks": [
            {
                "name": "unit-tests",
                "state": "FAILURE",
                "bucket": "fail",
                "link": details_url,
                "workflow": "CI",
                "description": "tests failed",
                "startedAt": "2026-07-14T01:00:00Z",
                "completedAt": "2026-07-14T01:01:00Z",
            }
        ],
        "check_runs": {
            "7001": {
                "id": 7001,
                "name": "unit-tests",
                "head_sha": head_sha,
                "status": "completed",
                "conclusion": "failure",
                "details_url": details_url,
                "html_url": details_url,
                "started_at": "2026-07-14T01:00:00Z",
                "completed_at": "2026-07-14T01:01:00Z",
                "output": {
                    "title": "Tests failed",
                    "summary": "pytest failed with token=super-secret-value",
                    "text": "See .env and -----BEGIN PRIVATE KEY-----\nPRIVATE-DATA\n-----END PRIVATE KEY-----",
                    "annotations_count": 1,
                },
                "app": {"name": "GitHub Actions"},
            }
        },
        "annotations": {
            "7001": [
                {
                    "path": "tests/test_demo.py",
                    "start_line": 12,
                    "end_line": 12,
                    "annotation_level": "failure",
                    "title": "Assertion failed",
                    "message": "AssertionError: expected 2, got 1; api_key=annotation-secret",
                    "raw_details": "long-token ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789abcdef",
                }
            ]
        },
        "jobs": {
            "7001": {
                "id": 7001,
                "run_id": 9001,
                "run_attempt": 2,
                "name": "unit-tests",
                "status": "completed",
                "conclusion": "failure",
                "html_url": details_url,
                "steps": [
                    {
                        "number": 1,
                        "name": "Checkout",
                        "status": "completed",
                        "conclusion": "success",
                    },
                    {
                        "number": 2,
                        "name": "Run pytest",
                        "status": "completed",
                        "conclusion": "failure",
                    },
                ],
            }
        },
        "logs": {
            "7001": "Run pytest\nFAILED tests/test_demo.py::test_value\npassword=log-secret\n.github/workflows/ci.yml: denied snippet\n"
        },
    }


def _update_state(env: ForgeEnvironment, additions: dict[str, object]) -> None:
    current = json.loads(env.gh_state.read_text(encoding="utf-8")) if env.gh_state.exists() else {}
    current.update(additions)
    env.gh_state.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def test_check_selectors_and_failure_evidence_are_sha_bound_and_secret_safe(
    forge_env: ForgeEnvironment,
) -> None:
    workspace_id, head_sha = _publish_workspace(forge_env)
    _update_state(forge_env, _failed_state(head_sha))

    checks = forge_env.service.workspace_pr_checks(workspace_id)
    check = checks["checks"][0]
    assert check["name"] == "unit-tests"
    assert check["bucket"] == "fail"
    assert check["selector"] == "check-run:7001"
    assert check["check_run_id"] == 7001
    assert check["head_sha"] == head_sha
    assert checks["pushed_sha"] == head_sha
    assert checks["stale"] is False

    details = forge_env.service.workspace_pr_check_details(workspace_id, check["selector"])
    assert details["check_run_id"] == 7001
    assert details["run_id"] == 9001
    assert details["job_id"] == 7001
    assert details["attempt"] == 2
    assert details["retried"] is True
    assert details["failed_step"] == "Run pytest"
    assert details["head_sha"] == head_sha
    assert details["stale"] is False
    assert details["annotations"][0]["path"] == "tests/test_demo.py"
    rendered_details = json.dumps(details, sort_keys=True)
    assert "annotation-secret" not in rendered_details
    assert "PRIVATE-DATA" not in rendered_details

    evidence = forge_env.service.workspace_pr_failure_evidence(
        workspace_id, check["selector"], max_excerpt_lines=4
    )
    assert evidence["failure_class"] == "test"
    assert evidence["retryable"] is False
    assert evidence["coverage"] == "complete"
    assert evidence["failed_step"] == "Run pytest"
    assert evidence["attempt"] == 2
    assert len(evidence["excerpt_sha256"]) == 64
    assert evidence["redacted"] is True
    assert evidence["withheld_lines"] >= 1
    rendered = json.dumps(evidence, sort_keys=True)
    for secret in (
        "super-secret-value",
        "annotation-secret",
        "log-secret",
        "PRIVATE-DATA",
        "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789abcdef",
        ".github/workflows/ci.yml",
    ):
        assert secret not in rendered

    audit_text = (forge_env.root / "state" / "audit.jsonl").read_text(encoding="utf-8")
    assert "annotation-secret" not in audit_text
    assert "log-secret" not in audit_text


def test_failure_evidence_returns_partial_coverage_when_optional_sources_are_unavailable(
    forge_env: ForgeEnvironment,
) -> None:
    workspace_id, head_sha = _publish_workspace(forge_env)
    state = _failed_state(head_sha)
    state["annotations"] = {"7001": []}
    state["annotations_permission_denied"] = True
    state["logs_permission_denied"] = True
    _update_state(forge_env, state)

    evidence = forge_env.service.workspace_pr_failure_evidence(
        workspace_id, "check-run:7001", max_excerpt_lines=10
    )
    assert evidence["coverage"] == "partial"
    assert evidence["failed_step"] == "Run pytest"
    assert evidence["source_errors"] == [
        "annotations_permission_denied",
        "job_log_permission_denied",
    ]
    assert evidence["uncertainty"]


def test_check_evidence_rejects_stale_sha_and_invalid_selector(
    forge_env: ForgeEnvironment,
) -> None:
    workspace_id, head_sha = _publish_workspace(forge_env)
    state = _failed_state("0" * len(head_sha))
    _update_state(forge_env, state)

    with pytest.raises(RepoForgeError) as stale:
        forge_env.service.workspace_pr_check_details(workspace_id, "check-run:7001")
    assert stale.value.code is ErrorCode.CHECK_EVIDENCE_STALE

    invalid = [
        "",
        "https://github.com/x",
        "job:7001",
        "check-run:-1",
        "check-run: 1",
        "check-run:99999999999999999999",
    ]
    for selector in invalid:
        with pytest.raises(RepoForgeError) as exc_info:
            forge_env.service.workspace_pr_check_details(workspace_id, selector)
        assert exc_info.value.code is ErrorCode.CHECK_SELECTOR_INVALID


def test_failure_evidence_uses_log_fallback_and_bounds_large_sources(
    forge_env: ForgeEnvironment,
) -> None:
    workspace_id, head_sha = _publish_workspace(forge_env)
    annotation_state = _failed_state(head_sha)
    annotation_state["annotations"] = {
        "7001": [
            {
                "path": f"tests/test_{index}.py",
                "start_line": index + 1,
                "end_line": index + 1,
                "annotation_level": "failure",
                "title": f"Failure {index}",
                "message": "x" * 2_000,
                "raw_details": "",
            }
            for index in range(60)
        ]
    }
    _update_state(forge_env, annotation_state)

    details = forge_env.service.workspace_pr_check_details(workspace_id, "check-run:7001")
    assert len(details["annotations"]) <= 50
    assert details["annotations_truncated"] is True
    assert details["truncated"] is True

    log_state = _failed_state(head_sha)
    run = log_state["check_runs"]["7001"]  # type: ignore[index]
    run["output"] = {"title": "", "summary": "", "text": "", "annotations_count": 0}  # type: ignore[index]
    log_state["annotations"] = {"7001": []}
    log_state["logs"] = {"7001": "pytest failure\n" + ("x" * 80_000)}
    _update_state(forge_env, log_state)

    evidence = forge_env.service.workspace_pr_failure_evidence(
        workspace_id,
        "check-run:7001",
        max_excerpt_lines=3,
    )
    assert evidence["failure_class"] == "test"
    assert "pytest failure" in evidence["excerpt"]
    assert len(evidence["excerpt"]) <= 64_100
    assert evidence["truncated"] is True
    assert len(evidence["excerpt"].splitlines()) <= 3


def test_non_failure_states_are_deterministic(forge_env: ForgeEnvironment) -> None:
    workspace_id, head_sha = _publish_workspace(forge_env)
    cases = [
        ("success", "completed", "pass", False),
        (None, "in_progress", "pending", False),
        ("skipped", "completed", "skipped", False),
        ("cancelled", "completed", "cancellation", True),
    ]
    for conclusion, status, expected_class, retryable in cases:
        state = _failed_state(head_sha)
        run = state["check_runs"]["7001"]  # type: ignore[index]
        run["conclusion"] = conclusion  # type: ignore[index]
        run["status"] = status  # type: ignore[index]
        run["output"] = {"title": "", "summary": "", "text": "", "annotations_count": 0}  # type: ignore[index]
        state["annotations"] = {"7001": []}
        state["logs"] = {"7001": ""}
        _update_state(forge_env, state)
        evidence = forge_env.service.workspace_pr_failure_evidence(workspace_id, "check-run:7001")
        assert evidence["failure_class"] == expected_class
        assert evidence["retryable"] is retryable
        assert evidence["excerpt"] == ""
        assert evidence["coverage"] == "none"


def test_selector_redaction_and_classification_helpers(forge_env: ForgeEnvironment) -> None:
    repo = forge_env.service.config.repositories["demo"]
    assert parse_check_selector("check-run:42") == 42
    sanitized = sanitize_ci_text(
        "Bearer abc.def\nhttps://user:password@example.com\n"
        "-----BEGIN PRIVATE KEY-----\nprivate\n-----END PRIVATE KEY-----\n"
        "token ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789abcdef\n"
        ".env: SECRET=value\n",
        repo,
        max_chars=10_000,
    )
    assert sanitized.redacted is True
    assert sanitized.withheld_lines == 1
    assert "\nprivate\n" not in sanitized.text
    assert "password@example.com" not in sanitized.text
    assert "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789abcdef" not in sanitized.text
    assert ".env" not in sanitized.text

    assert classify_ci_failure(["pytest AssertionError"]).failure_class == "test"
    assert classify_ci_failure(["ruff lint failed"]).failure_class == "lint"
    assert classify_ci_failure(["mypy type error"]).failure_class == "type"
    assert classify_ci_failure(["build wheel failed"]).failure_class == "build"
    assert classify_ci_failure(["dependency lock resolution failed"]).failure_class == "dependency"
    assert classify_ci_failure(["runner image unavailable"]).failure_class == "environment"
    assert classify_ci_failure(["operation timed out"]).failure_class == "timeout"
    assert classify_ci_failure(["policy denied workflow"]).failure_class == "policy"
    assert classify_ci_failure(["connection reset by peer"]).failure_class == "network"
    assert classify_ci_failure(["job cancelled"]).failure_class == "cancellation"
