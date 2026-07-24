from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from conftest import ForgeEnvironment
from mcp.shared.memory import create_connected_server_and_client_session

from repoforge.adapters.persistence.json_pr_check_watch_store import (
    JsonPrCheckWatchStore,
)
from repoforge.application.operations.recovery import recover_operations
from repoforge.application.workspace.edit import FileEdit, TextEdit
from repoforge.application.workspace.pr_watch import (
    PrCheckWatchCoordinator,
    WorkspacePrWatchCommand,
)
from repoforge.domain.errors import ErrorCode, RepoForgeError
from repoforge.domain.pr_check_watch import (
    PR_CHECK_WATCH_SCHEMA_VERSION,
    PrCheckWatch,
    PrCheckWatchOutcome,
    PrCheckWatchUntil,
    new_pr_check_watch,
    update_pr_check_watch,
)
from repoforge.interfaces.mcp.server import create_server
from repoforge.testing.fakes import (
    InMemoryLockManager,
    ManualBackgroundTaskRunner,
    RecordingSleeper,
)


def _record() -> PrCheckWatch:
    return new_pr_check_watch(
        operation_id="op-000000000000000000000001",
        workspace_id="workspace-1",
        branch="ai/example-1234567890",
        pr_number=42,
        pushed_sha="a" * 40,
        workspace_fingerprint="b" * 64,
        remote_version="prv2:" + "c" * 64,
        stability_version="prm2:" + "d" * 64,
        until=PrCheckWatchUntil.ALL_COMPLETED,
        include_failure_evidence=True,
        timeout_seconds=300,
        created_at="2026-07-14T00:00:00+00:00",
        deadline_at="2026-07-14T00:05:00+00:00",
    )


def _updated(watch: PrCheckWatch) -> PrCheckWatch:
    return update_pr_check_watch(
        watch,
        now="2026-07-14T00:00:01+00:00",
        poll_count=1,
        pass_count=1,
        fail_count=0,
        pending_count=2,
        skipping_count=1,
        selectors=("check-run:102", "check-run:101", "check-run:101"),
        failed_selectors=(),
        evidence_references=(),
        next_delay_seconds=2,
        provider_error_code=None,
        outcome=PrCheckWatchOutcome.PENDING,
    )


def _check(bucket: str, state: str) -> dict[str, str]:
    return {
        "name": "unit",
        "state": state,
        "bucket": bucket,
        "link": "https://github.com/owner/demo/actions/runs/1001/job/101",
        "workflow": "CI",
        "description": bucket,
        "startedAt": "",
        "completedAt": "",
    }


def _published_workspace(env: ForgeEnvironment) -> str:
    created = env.service.workspace_create("demo", "watch checks")
    workspace_id = created["workspace_id"]
    current = env.service.workspace_read_file(workspace_id, "hello.txt")
    env.service.workspace_edit(
        workspace_id,
        [FileEdit("hello.txt", current["sha256"], (TextEdit("hello", "changed for watch"),))],
    )
    env.service.workspace_run_profile(workspace_id)
    env.service.workspace_commit(workspace_id, "Prepare PR watch")
    env.service.workspace_push(workspace_id)
    env.service.workspace_create_draft_pr(workspace_id, "Watch checks", "Test body")
    return workspace_id


def _coordinator(
    env: ForgeEnvironment,
) -> tuple[PrCheckWatchCoordinator, ManualBackgroundTaskRunner]:
    runner = ManualBackgroundTaskRunner()
    coordinator = PrCheckWatchCoordinator(
        env.service.application.context,
        env.service.operations,
        JsonPrCheckWatchStore(
            env.service.config.server.state_root,
            env.service.application.context.locks,
        ),
        runner,
        RecordingSleeper(),
    )
    return coordinator, runner


def test_watch_domain_is_bounded_deterministic_and_monotonic() -> None:
    watch = _record()
    assert watch.schema_version == PR_CHECK_WATCH_SCHEMA_VERSION == 2
    assert watch.until is PrCheckWatchUntil.ALL_COMPLETED
    updated = _updated(watch)
    assert updated.selectors == ("check-run:101", "check-run:102")
    assert updated.poll_count == 1
    assert updated.updated_at > watch.updated_at

    with pytest.raises(RepoForgeError) as invalid:
        new_pr_check_watch(
            operation_id=watch.operation_id,
            workspace_id=watch.workspace_id,
            branch=watch.branch,
            pr_number=watch.pr_number,
            pushed_sha=watch.pushed_sha,
            workspace_fingerprint=watch.workspace_fingerprint,
            remote_version=watch.remote_version,
            stability_version=watch.stability_version,
            until=watch.until,
            include_failure_evidence=True,
            timeout_seconds=4,
            created_at=watch.created_at,
            deadline_at=watch.deadline_at,
        )
    assert invalid.value.code is ErrorCode.PR_CHECK_WATCH_INVALID

    with pytest.raises(RepoForgeError):
        update_pr_check_watch(
            updated,
            now="2026-07-14T00:00:02+00:00",
            poll_count=0,
            pass_count=0,
            fail_count=0,
            pending_count=0,
            skipping_count=0,
            selectors=(),
            failed_selectors=(),
            evidence_references=(),
            next_delay_seconds=1,
            provider_error_code=None,
            outcome=PrCheckWatchOutcome.PENDING,
        )


def test_json_watch_store_is_private_atomic_cas_and_strict(tmp_path: Path) -> None:
    store = JsonPrCheckWatchStore(tmp_path, InMemoryLockManager())
    watch = _record()
    assert store.create(watch) == watch
    path = tmp_path / "pr-check-watches" / f"{watch.operation_id}.json"
    assert os.stat(path.parent).st_mode & 0o777 == 0o700
    assert os.stat(path).st_mode & 0o777 == 0o600
    assert store.read(watch.operation_id) == watch
    assert store.encode_for_test(watch) == store.encode_for_test(watch)

    updated = _updated(watch)
    saved = store.save(updated, expected_updated_at=watch.updated_at)
    assert saved == updated
    with pytest.raises(RepoForgeError) as stale:
        store.save(updated, expected_updated_at=watch.updated_at)
    assert stale.value.code is ErrorCode.PR_CHECK_WATCH_STALE

    payload = json.loads(store.encode_for_test(updated))
    payload["schema_version"] = 99
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(RepoForgeError) as future:
        store.read(watch.operation_id)
    assert future.value.code is ErrorCode.PR_CHECK_WATCH_STATE_CORRUPT

    payload["schema_version"] = PR_CHECK_WATCH_SCHEMA_VERSION
    payload["stdout"] = "secret"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(RepoForgeError):
        store.read(watch.operation_id)


def test_watch_completes_after_pending_checks_finish(forge_env: ForgeEnvironment) -> None:
    workspace_id = _published_workspace(forge_env)
    state = json.loads(forge_env.gh_state.read_text(encoding="utf-8"))
    state["checks"] = [_check("pending", "PENDING")]
    forge_env.gh_state.write_text(json.dumps(state), encoding="utf-8")
    coordinator, runner = _coordinator(forge_env)

    result = coordinator.start(WorkspacePrWatchCommand(workspace_id, "all_completed", 300, True))
    assert result.operation.kind == "pr_check_watch"
    assert result.operation.state == "running"
    assert runner.keys == (result.operation.operation_id,)
    watch = coordinator.store.read(result.operation.operation_id)
    assert watch is not None
    assert coordinator.run_once(watch.operation_id) is False

    state["checks"] = [_check("pass", "SUCCESS")]
    forge_env.gh_state.write_text(json.dumps(state), encoding="utf-8")
    assert coordinator.run_once(watch.operation_id) is True
    status = forge_env.service.operation_status(watch.operation_id)
    assert status["state"] == "succeeded"
    completed = coordinator.store.read(watch.operation_id)
    assert completed is not None
    assert completed.outcome is PrCheckWatchOutcome.ALL_COMPLETED
    assert completed.selectors == ("check-run:101",)


def test_watch_first_failure_cancellation_and_stale_identity(
    forge_env: ForgeEnvironment,
) -> None:
    workspace_id = _published_workspace(forge_env)
    state = json.loads(forge_env.gh_state.read_text(encoding="utf-8"))
    state["checks"] = [_check("fail", "FAILURE")]
    forge_env.gh_state.write_text(json.dumps(state), encoding="utf-8")
    coordinator, _runner = _coordinator(forge_env)

    failed = coordinator.start(WorkspacePrWatchCommand(workspace_id, "first_failure", 300, True))
    assert coordinator.run_once(failed.operation.operation_id) is True
    failed_watch = coordinator.store.read(failed.operation.operation_id)
    assert failed_watch is not None
    assert failed_watch.outcome is PrCheckWatchOutcome.FIRST_FAILURE
    assert failed_watch.failed_selectors == ("check-run:101",)
    assert failed_watch.evidence_references == ("check-run:101",)

    state["checks"] = [_check("pending", "PENDING")]
    forge_env.gh_state.write_text(json.dumps(state), encoding="utf-8")
    cancelled = coordinator.start(
        WorkspacePrWatchCommand(workspace_id, "all_completed", 300, False)
    )
    forge_env.service.operation_cancel(cancelled.operation.operation_id)
    assert coordinator.run_once(cancelled.operation.operation_id) is True
    cancelled_status = forge_env.service.operation_status(cancelled.operation.operation_id)
    assert cancelled_status["state"] == "cancelled"

    stale = coordinator.start(WorkspacePrWatchCommand(workspace_id, "all_completed", 300, False))
    workspace_path = Path(forge_env.service.workspace_status(workspace_id)["path"])
    (workspace_path / "scratch.txt").write_text("dirty\n", encoding="utf-8")
    assert coordinator.run_once(stale.operation.operation_id) is True
    stale_status = forge_env.service.operation_status(stale.operation.operation_id)
    assert stale_status["state"] == "failed"
    assert stale_status["error_code"] == ErrorCode.PR_CHECK_WATCH_STALE.value


def _audit_events(root: Path, action: str) -> list[dict[str, object]]:
    audit_path = root / "state" / "audit.jsonl"
    events = [
        json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines() if line
    ]
    return [event for event in events if event["action"] == action]


def test_workspace_pr_watch_registration_produces_exactly_one_bounded_audit_event(
    forge_env: ForgeEnvironment,
) -> None:
    workspace_id = _published_workspace(forge_env)
    coordinator, _runner = _coordinator(forge_env)

    result = coordinator.start(WorkspacePrWatchCommand(workspace_id, "all_completed", 300, True))

    events = _audit_events(forge_env.root, "workspace_pr_watch")
    assert len(events) == 1
    event = events[0]
    assert event["success"] is True
    details = event["details"]
    assert details["workspace_id"] == workspace_id
    assert details["until"] == "all_completed"
    assert details["timeout_seconds"] == 300
    assert details["include_failure_evidence"] is True
    assert details["expected_remote_version"] is False
    assert details["operation_id"] == result.operation.operation_id
    # Bounded: a PR number is an identifier, never the PR title/body or check output.
    assert set(details) == {
        "workspace_id",
        "until",
        "timeout_seconds",
        "include_failure_evidence",
        "expected_remote_version",
        "operation_id",
        "pr_number",
        "deadline_at",
        "correlation_id",
        "duration_ms",
        "result_bytes",
        "is_mutating",
        "origin",
        "correlation_hash",
        "repo_id",
    }
    assert "Watch checks" not in json.dumps(details)
    assert "Test body" not in json.dumps(details)


def test_workspace_pr_watch_audits_failure_when_the_commit_was_never_pushed(
    forge_env: ForgeEnvironment,
) -> None:
    created = forge_env.service.workspace_create("demo", "unpushed watch")
    workspace_id = created["workspace_id"]
    coordinator, _runner = _coordinator(forge_env)

    with pytest.raises(RepoForgeError) as exc:
        coordinator.start(WorkspacePrWatchCommand(workspace_id, "all_completed", 300, True))
    assert exc.value.code is ErrorCode.PR_CHECK_WATCH_STALE

    events = _audit_events(forge_env.root, "workspace_pr_watch")
    assert len(events) == 1
    event = events[0]
    assert event["success"] is False
    assert event["details"]["workspace_id"] == workspace_id
    assert event["details"]["error_code"] == ErrorCode.PR_CHECK_WATCH_STALE.value
    # No operation/PR identifiers exist yet at this failure point; nothing beyond inputs leaked.
    assert "operation_id" not in event["details"]
    assert "pr_number" not in event["details"]


def test_watch_recovery_preserves_and_reschedules_resumable_work(
    forge_env: ForgeEnvironment,
) -> None:
    workspace_id = _published_workspace(forge_env)
    state = json.loads(forge_env.gh_state.read_text(encoding="utf-8"))
    state["checks"] = [_check("pending", "PENDING")]
    forge_env.gh_state.write_text(json.dumps(state), encoding="utf-8")
    coordinator, runner = _coordinator(forge_env)
    result = coordinator.start(WorkspacePrWatchCommand(workspace_id, "all_completed", 300, False))

    report = recover_operations(
        forge_env.service.operations,
        now=forge_env.service.application.context.clock.now_iso(),
        resumable_kinds=frozenset({"pr_check_watch"}),
    )
    assert report.orphaned == 0
    assert forge_env.service.operation_status(result.operation.operation_id)["state"] == "running"
    assert coordinator.resume_active() == ()
    assert runner.keys == (result.operation.operation_id,)


@pytest.mark.anyio
async def test_workspace_pr_watch_is_exposed_through_actual_mcp(
    forge_env: ForgeEnvironment,
) -> None:
    workspace_id = _published_workspace(forge_env)
    server = create_server(service=forge_env.service)
    async with create_connected_server_and_client_session(server) as session:
        tools = {tool.name: tool for tool in (await session.list_tools()).tools}
        tool = tools["workspace_pr"]
        assert tool.annotations.readOnlyHint is False
        assert tool.annotations.destructiveHint is False
        assert tool.annotations.openWorldHint is True
        evidence = await session.call_tool(
            "workspace_pr_evidence",
            {"workspace_id": workspace_id},
        )
        assert evidence.isError is False
        assert evidence.structuredContent is not None
        result = await session.call_tool(
            "workspace_pr",
            {
                "workspace_id": workspace_id,
                "action": "watch",
                "expected_remote_version": evidence.structuredContent["remote_version"],
                "until": "all_completed",
                "timeout_seconds": 300,
            },
        )
        assert result.isError is False
        assert result.structuredContent["operation"]["kind"] == "pr_check_watch"


def _seed_failed_ci_check(
    forge_env: ForgeEnvironment,
    *,
    workspace_id: str,
    check_run_id: int = 501,
    job_id: int = 900,
) -> None:
    ctx = forge_env.service.application.context
    record = ctx.store.load(workspace_id)
    workspace = Path(forge_env.service.workspace_status(workspace_id)["path"])
    head_sha = ctx.git.head_sha(workspace)
    record.metadata["last_pushed_sha"] = head_sha
    ctx.store.save(record)
    state = {
        "prs": {
            record.branch: {
                "number": 42,
                "title": "Failure evidence",
                "body": "Failure evidence body",
                "url": "https://github.com/owner/demo/pull/42",
                "state": "OPEN",
                "isDraft": True,
                "mergeable": "MERGEABLE",
                "reviewDecision": "",
                "statusCheckRollup": [],
                "comments": [],
                "reviews": [],
                "updatedAt": "2026-07-21T14:00:00Z",
                "headRefOid": head_sha,
            }
        },
        "check_runs": {
            str(check_run_id): {
                "id": check_run_id,
                "name": "tests (ubuntu-latest, 3.10)",
                "head_sha": head_sha,
                "status": "completed",
                "conclusion": "failure",
                "details_url": (f"https://github.com/owner/demo/actions/runs/800/job/{job_id}"),
                "html_url": f"https://github.com/owner/demo/actions/runs/800/job/{job_id}",
                "started_at": "2026-07-21T13:07:11Z",
                "completed_at": "2026-07-21T13:14:06Z",
                "output": {
                    "title": "",
                    "summary": "",
                    "text": "",
                    "annotations_count": 1,
                },
                "app": {"name": "GitHub Actions"},
            }
        },
        "annotations": {
            str(check_run_id): [
                {
                    "path": ".github",
                    "start_line": 1014,
                    "end_line": 1014,
                    "annotation_level": "failure",
                    "title": "",
                    "message": "Process completed with exit code 1.",
                    "raw_details": "",
                }
            ]
        },
        "jobs": {
            str(job_id): {
                "id": job_id,
                "run_id": 800,
                "run_attempt": 1,
                "name": "tests (ubuntu-latest, 3.10)",
                "status": "completed",
                "conclusion": "failure",
                "html_url": f"https://github.com/owner/demo/actions/runs/800/job/{job_id}",
                "steps": [
                    {
                        "number": 4,
                        "name": "Run pytest",
                        "status": "completed",
                        "conclusion": "failure",
                    }
                ],
            }
        },
        "logs": {
            str(job_id): (
                "FAILED tests/test_alpha.py::test_one - AssertionError: expected true\n"
                "FAILED tests/test_beta.py::test_two - ValueError: broken\n"
                "tests/test_alpha.py:12: AssertionError\n"
                "2 failed in 0.42s\n"
            )
        },
    }
    forge_env.gh_state.write_text(json.dumps(state), encoding="utf-8")


def test_ci_log_projection(
    forge_env: ForgeEnvironment,
) -> None:
    workspace_id = forge_env.service.workspace_create(
        "demo", "generic annotation job log fallback"
    )["workspace_id"]
    _seed_failed_ci_check(forge_env, workspace_id=workspace_id)

    evidence = forge_env.service.workspace_pr_failure_evidence(
        workspace_id,
        "check-run:501",
        max_excerpt_lines=80,
    )

    assert evidence["failed_step"] == "Run pytest"
    assert evidence["failure_provider"] == "pytest"
    assert evidence["selector_coverage"] == "complete"
    assert evidence["selectors_unavailable_reason"] is None
    assert evidence["failed_selectors"] == [
        "tests/test_alpha.py::test_one",
        "tests/test_beta.py::test_two",
    ]
    assert evidence["failure_locations"] == []
    assert "FAILED tests/test_alpha.py::test_one" in evidence["excerpt"]
    assert evidence["coverage"] == "complete"
    assert evidence["output_artifact_status"] == "available"
    reference = evidence["output_artifact_reference"]
    assert reference.startswith("failure-output:")
    digest = reference.removeprefix("failure-output:")
    artifact = (
        forge_env.service.config.server.state_root / "failure-output-artifacts" / f"{digest}.blob"
    )
    artifact_body = artifact.read_text(encoding="utf-8")
    assert "FAILED tests/test_alpha.py::test_one" in artifact_body
    assert "FAILED tests/test_beta.py::test_two" in artifact_body

    public = forge_env.service.workspace_pr_evidence(
        workspace_id,
        detail="failure",
        check_selector="check-run:501",
        max_excerpt_lines=80,
    )
    from repoforge.contracts.registry import V2_TOOL_SPECS

    V2_TOOL_SPECS["workspace_pr_evidence"].validate_output(public)
    assert public["failure_provider"] == "pytest"
    assert public["selector_coverage"] == "complete"
    assert public["failed_selectors"] == evidence["failed_selectors"]
    assert public["output_artifact_reference"] == reference
    assert public["output_artifact_status"] == "available"


def test_ci_log_source_unavailable_is_explicit(
    forge_env: ForgeEnvironment,
) -> None:
    workspace_id = forge_env.service.workspace_create("demo", "ci log source unavailable")[
        "workspace_id"
    ]
    _seed_failed_ci_check(forge_env, workspace_id=workspace_id)
    state = json.loads(forge_env.gh_state.read_text(encoding="utf-8"))
    state["logs_permission_denied"] = True
    forge_env.gh_state.write_text(json.dumps(state), encoding="utf-8")

    evidence = forge_env.service.workspace_pr_failure_evidence(
        workspace_id,
        "check-run:501",
        max_excerpt_lines=80,
    )

    assert evidence["coverage"] == "partial"
    assert evidence["failure_provider"] == "custom"
    assert evidence["selector_coverage"] == "unavailable"
    assert evidence["selectors_unavailable_reason"] == "output_unrecognized"
    assert evidence["failed_selectors"] == []
    assert evidence["output_artifact_status"] == "source_unavailable"
    assert evidence["output_artifact_reference"].startswith("failure-output:")
    assert any(item.startswith("job_log_") for item in evidence["source_errors"])
