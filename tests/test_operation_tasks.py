from __future__ import annotations

import importlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import ForgeEnvironment
from mcp.shared.memory import create_connected_server_and_client_session

from repoforge.adapters.persistence.json_operation_result_store import JsonOperationResultStore
from repoforge.adapters.persistence.json_operation_store import JsonOperationStore
from repoforge.application.operations.recovery import recover_operations
from repoforge.application.service import CodingService
from repoforge.config import load_config
from repoforge.domain.errors import ErrorCode, RepoForgeError
from repoforge.domain.operation_task import (
    OPERATION_SCHEMA_VERSION,
    OperationSnapshotBinding,
    OperationState,
    new_operation_task,
    request_operation_cancellation,
    transition_operation,
    update_operation_progress,
)
from repoforge.interfaces.mcp.server import create_server
from repoforge.testing.fakes import FixedClock, InMemoryLockManager, InMemoryOperationStore


def _task(
    *,
    operation_id: str = "op-000000000000000000000001",
    now: str = "2026-07-14T00:00:00+00:00",
    cancel_supported: bool = True,
):
    return new_operation_task(
        operation_id=operation_id,
        kind="pr_check_watch",
        phase="queued",
        now=now,
        cancel_supported=cancel_supported,
        task_id="task-1",
        workspace_id="workspace-1",
        snapshot_binding=OperationSnapshotBinding(
            head_sha="a" * 40,
            workspace_fingerprint="b" * 64,
            config_generation=3,
            evidence_snapshot_id="evidence-1",
        ),
        expires_at="2026-07-15T00:00:00+00:00",
    )


def test_operation_domain_models_every_transition_and_progress_rule() -> None:
    pending = _task()
    assert pending.schema_version == OPERATION_SCHEMA_VERSION == 1
    assert pending.state is OperationState.PENDING
    assert pending.progress_current == 0
    assert pending.snapshot_binding is not None
    assert pending.snapshot_binding.config_generation == 3
    with pytest.raises(RepoForgeError):
        new_operation_task(
            operation_id="op-000000000000000000000123",
            kind="watch",
            phase="queued",
            now="2026-07-14T00:00:00+00:00",
            cancel_supported=1,  # type: ignore[arg-type]
        )

    allowed_from_pending = {
        OperationState.RUNNING,
        OperationState.FAILED,
        OperationState.CANCELLED,
        OperationState.EXPIRED,
    }
    for index, state in enumerate(
        sorted(allowed_from_pending, key=lambda item: item.value), start=1
    ):
        transitioned = transition_operation(
            pending,
            state,
            now=f"2026-07-14T00:00:0{index}+00:00",
            error_code="FAIL" if state is OperationState.FAILED else None,
            error_message=(
                "token=domain-secret\n"
                "-----BEGIN PRIVATE KEY-----\nprivate-material\n-----END PRIVATE KEY-----\n"
                "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789abcdef"
                if state is OperationState.FAILED
                else None
            ),
        )
        assert transitioned.state is state
        if state is OperationState.FAILED:
            rendered_error = transitioned.error_message or ""
            assert "domain-secret" not in rendered_error
            assert "private-material" not in rendered_error
            assert "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789abcdef" not in rendered_error

    running = transition_operation(pending, OperationState.RUNNING, now="2026-07-14T00:00:10+00:00")
    for index, state in enumerate(
        (
            OperationState.SUCCEEDED,
            OperationState.FAILED,
            OperationState.CANCELLED,
            OperationState.EXPIRED,
            OperationState.ORPHANED,
        ),
        start=11,
    ):
        transitioned = transition_operation(
            running,
            state,
            now=f"2026-07-14T00:00:{index}+00:00",
            result_reference="result-1" if state is OperationState.SUCCEEDED else None,
            error_code="WORKER_LOST" if state is OperationState.ORPHANED else None,
        )
        assert transitioned.state is state

    same = transition_operation(running, OperationState.RUNNING, now=running.updated_at)
    assert same == running
    succeeded = transition_operation(
        running,
        OperationState.SUCCEEDED,
        now="2026-07-14T00:01:00+00:00",
        result_reference="result-1",
    )
    with pytest.raises(RepoForgeError) as invalid_transition:
        transition_operation(succeeded, OperationState.RUNNING, now="2026-07-14T00:02:00+00:00")
    assert invalid_transition.value.code is ErrorCode.OPERATION_TRANSITION_INVALID

    progressed = update_operation_progress(
        running,
        phase="checking",
        current=2,
        total=5,
        unit="checks",
        message="2 of 5",
        now="2026-07-14T00:00:20+00:00",
    )
    later = update_operation_progress(
        progressed,
        phase="checking",
        current=3,
        total=5,
        unit="checks",
        message="3 of 5",
        now="2026-07-14T00:00:21+00:00",
    )
    assert later.progress_current == 3
    reset = update_operation_progress(
        later,
        phase="summarizing",
        current=0,
        total=1,
        unit="stage",
        message="new phase",
        now="2026-07-14T00:00:22+00:00",
    )
    assert reset.progress_current == 0
    with pytest.raises(RepoForgeError) as backwards:
        update_operation_progress(
            later,
            phase="checking",
            current=1,
            total=5,
            now="2026-07-14T00:00:23+00:00",
        )
    assert backwards.value.code is ErrorCode.OPERATION_INVALID
    with pytest.raises(RepoForgeError):
        update_operation_progress(
            later,
            phase="checking",
            current=6,
            total=5,
            now="2026-07-14T00:00:24+00:00",
        )


def test_cancellation_request_is_distinct_idempotent_and_bounded() -> None:
    running = transition_operation(_task(), OperationState.RUNNING, now="2026-07-14T00:00:01+00:00")
    first = request_operation_cancellation(running, now=running.updated_at)
    assert first.cancellation_requested is True
    assert first.already_requested is False
    assert first.already_terminal is False
    assert first.task.state is OperationState.RUNNING
    assert first.task.cancellation_requested_at is not None
    assert first.task.updated_at > running.updated_at

    second = request_operation_cancellation(first.task, now=first.task.updated_at)
    assert second.task == first.task
    assert second.already_requested is True

    unsupported = request_operation_cancellation(
        transition_operation(
            _task(cancel_supported=False),
            OperationState.RUNNING,
            now="2026-07-14T00:00:01+00:00",
        ),
        now="2026-07-14T00:00:02+00:00",
    )
    assert unsupported.cancellation_requested is False
    assert unsupported.cancel_supported is False

    terminal = transition_operation(
        running,
        OperationState.SUCCEEDED,
        now="2026-07-14T00:00:03+00:00",
        result_reference="result-1",
    )
    terminal_decision = request_operation_cancellation(terminal, now="2026-07-14T00:00:04+00:00")
    assert terminal_decision.task == terminal
    assert terminal_decision.already_terminal is True


def test_json_operation_store_is_private_atomic_and_compare_and_swap(tmp_path: Path) -> None:
    locks = InMemoryLockManager()
    store = JsonOperationStore(tmp_path, locks)
    task = _task()
    created = store.create(task)
    assert created == task
    root = tmp_path / "operations"
    path = root / f"{task.operation_id}.json"
    assert path.is_file()
    assert os.stat(root).st_mode & 0o777 == 0o700
    assert os.stat(path).st_mode & 0o777 == 0o600
    assert path.read_bytes() == JsonOperationStore.encode_for_test(task)
    assert store.read(task.operation_id) == task
    assert JsonOperationStore(tmp_path, InMemoryLockManager()).read(task.operation_id) == task

    with pytest.raises(RepoForgeError) as duplicate:
        store.create(task)
    assert duplicate.value.code is ErrorCode.ALREADY_EXISTS

    running = transition_operation(task, OperationState.RUNNING, now="2026-07-14T00:00:01+00:00")
    saved = store.save(running, expected_updated_at=task.updated_at)
    assert saved == running
    with pytest.raises(RepoForgeError) as stale:
        store.save(
            transition_operation(
                running,
                OperationState.FAILED,
                now="2026-07-14T00:00:02+00:00",
                error_code="FAIL",
            ),
            expected_updated_at=task.updated_at,
        )
    assert stale.value.code is ErrorCode.OPERATION_STALE

    page = store.list_records(max_records=1)
    assert page.records == (running,)
    assert page.scan_truncated is False
    assert not list(root.glob("*.tmp*"))

    store.delete(task.operation_id)
    assert store.read(task.operation_id) is None


def test_json_operation_result_store_is_private_bounded_and_restart_safe(tmp_path: Path) -> None:
    operation_id = "op-000000000000000000000001"
    locks = InMemoryLockManager()
    store = JsonOperationResultStore(tmp_path, locks, max_result_bytes=1_000)
    result = {
        "workspace_id": "workspace-1",
        "profile": "full",
        "commands": [{"argv": ["pytest"], "returncode": 0, "stdout": "ok", "stderr": ""}],
    }

    store.save(operation_id, result)
    root = tmp_path / "operation-results"
    path = root / f"{operation_id}.json"
    assert path.is_file()
    assert os.stat(root).st_mode & 0o777 == 0o700
    assert os.stat(path).st_mode & 0o777 == 0o600
    assert store.read(operation_id) == result
    assert JsonOperationResultStore(tmp_path, InMemoryLockManager()).read(operation_id) == result

    with pytest.raises(RepoForgeError) as oversized:
        JsonOperationResultStore(tmp_path, locks, max_result_bytes=128).save(
            operation_id,
            {"payload": "x" * 1_000},
        )
    assert oversized.value.code is ErrorCode.STATE_TOO_LARGE

    store.delete(operation_id)
    assert store.read(operation_id) is None


def test_json_operation_store_rejects_corruption_future_schema_and_identity_mismatch(
    tmp_path: Path,
) -> None:
    store = JsonOperationStore(tmp_path, InMemoryLockManager())
    task = _task()
    store.create(task)
    path = tmp_path / "operations" / f"{task.operation_id}.json"

    path.write_text("{bad", encoding="utf-8")
    with pytest.raises(RepoForgeError) as corrupt:
        store.read(task.operation_id)
    assert corrupt.value.code is ErrorCode.OPERATION_CORRUPT

    payload = json.loads(store.encode_for_test(task).decode())
    payload["schema_version"] = 99
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(RepoForgeError) as future:
        store.read(task.operation_id)
    assert future.value.code is ErrorCode.OPERATION_SCHEMA_UNSUPPORTED

    payload["schema_version"] = True
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(RepoForgeError) as boolean_schema:
        store.read(task.operation_id)
    assert boolean_schema.value.code is ErrorCode.OPERATION_SCHEMA_UNSUPPORTED

    payload["schema_version"] = 1
    payload["operation_id"] = "op-000000000000000000000002"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(RepoForgeError) as mismatch:
        store.read(task.operation_id)
    assert mismatch.value.code is ErrorCode.OPERATION_CORRUPT

    payload["operation_id"] = task.operation_id
    payload["stdout"] = "secret"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(RepoForgeError) as forbidden:
        store.read(task.operation_id)
    assert forbidden.value.code is ErrorCode.OPERATION_CORRUPT


def test_internal_manager_public_status_list_and_cancel(forge_env: ForgeEnvironment) -> None:
    manager = forge_env.service.operations
    first = manager.create(
        kind="pr_check_watch",
        phase="queued",
        cancel_supported=True,
        workspace_id="workspace-1",
        task_id="task-1",
        snapshot_binding=OperationSnapshotBinding(
            head_sha="c" * 40,
            workspace_fingerprint="d" * 64,
            config_generation=7,
            evidence_snapshot_id="evidence-public",
        ),
    )
    manager.start(first.operation_id)
    current = manager.progress(
        first.operation_id,
        phase="polling",
        current=1,
        total=3,
        unit="checks",
        message="token=manager-secret",
    )
    assert "manager-secret" not in (current.progress_message or "")

    second = manager.create(
        kind="verification",
        phase="queued",
        cancel_supported=False,
        workspace_id="workspace-2",
        task_id="task-1",
    )
    manager.start(second.operation_id)

    status = forge_env.service.operation_status(first.operation_id)
    assert status["operation_id"] == first.operation_id
    assert status["state"] == "running"
    assert status["phase"] == "polling"
    assert status["progress"]["current"] == 1
    assert status["snapshot_binding"] == {
        "head_sha": "c" * 40,
        "workspace_fingerprint": "d" * 64,
        "config_generation": 7,
        "evidence_snapshot_id": "evidence-public",
    }

    listed = forge_env.service.operation_list(scope="task:task-1", state="running", limit=1)
    assert len(listed["operations"]) == 1
    assert listed["next_cursor"] is not None
    resumed = forge_env.service.operation_list(
        scope="task:task-1",
        state="running",
        limit=1,
        cursor=listed["next_cursor"],
    )
    assert len(resumed["operations"]) == 1
    assert resumed["operations"][0]["operation_id"] != listed["operations"][0]["operation_id"]

    cancelled = forge_env.service.operation_cancel(
        first.operation_id,
        expected_updated_at=current.updated_at,
    )
    assert cancelled["cancellation_requested"] is True
    assert cancelled["operation"]["state"] == "running"
    repeated = forge_env.service.operation_cancel(first.operation_id)
    assert repeated["already_requested"] is True

    unsupported = forge_env.service.operation_cancel(second.operation_id)
    assert unsupported["cancel_supported"] is False
    assert unsupported["cancellation_requested"] is False

    with pytest.raises(RepoForgeError) as stale:
        forge_env.service.operation_cancel(first.operation_id, expected_updated_at=first.updated_at)
    assert stale.value.code is ErrorCode.OPERATION_STALE
    with pytest.raises(RepoForgeError) as cursor:
        forge_env.service.operation_list(cursor="op-ffffffffffffffffffffffff")
    assert cursor.value.code is ErrorCode.OPERATION_INVALID
    with pytest.raises(RepoForgeError):
        forge_env.service.operation_list(scope="repo:demo")
    with pytest.raises(RepoForgeError):
        forge_env.service.operation_list(scope="task:../escape")
    with pytest.raises(RepoForgeError):
        forge_env.service.operation_list(limit=True)

    audit = (forge_env.root / "state" / "audit.jsonl").read_text(encoding="utf-8")
    assert "manager-secret" not in audit
    assert "progress_message" not in audit


def test_restart_recovery_orphans_running_expires_due_and_prunes_old_terminal(
    forge_env: ForgeEnvironment,
) -> None:
    manager = forge_env.service.operations
    running = manager.create(kind="watch", phase="polling", cancel_supported=True)
    manager.start(running.operation_id)
    due = manager.create(
        kind="index",
        phase="queued",
        cancel_supported=True,
        expires_at="2026-07-14T00:00:00+00:00",
    )
    old = _task(operation_id="op-000000000000000000000099", now="2026-07-01T00:00:00+00:00")
    old = transition_operation(
        old,
        OperationState.RUNNING,
        now="2026-07-01T00:00:01+00:00",
    )
    old = transition_operation(
        old,
        OperationState.SUCCEEDED,
        now="2026-07-01T00:00:02+00:00",
        result_reference="result-old",
    )
    forge_env.service.application.context.operation_store.create(old)

    report = recover_operations(
        manager,
        now="2026-07-14T00:00:01+00:00",
        retention_seconds=7 * 24 * 60 * 60,
    )
    assert report.orphaned == 1
    assert report.expired == 1
    assert report.deleted == 1
    assert manager.status(running.operation_id).state is OperationState.ORPHANED
    assert manager.status(due.operation_id).state is OperationState.EXPIRED
    assert forge_env.service.application.context.operation_store.read(old.operation_id) is None
    audit = (forge_env.root / "state" / "audit.jsonl").read_text(encoding="utf-8")
    audit_records = [json.loads(line) for line in audit.splitlines() if line]
    assert any(record.get("action") == "operation_delete" for record in audit_records)
    assert "result-old" not in audit

    # Building a fresh service is restart-safe and does not resurrect terminal state.
    restarted = CodingService(load_config(forge_env.config_path))
    assert restarted.operation_status(running.operation_id)["state"] == "orphaned"


def _audit_events(root: Path, action: str) -> list[dict[str, object]]:
    audit_path = root / "state" / "audit.jsonl"
    events = [
        json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines() if line
    ]
    return [event for event in events if event["action"] == action]


def test_operation_status_list_cancel_each_produce_exactly_one_audit_event(
    forge_env: ForgeEnvironment,
) -> None:
    manager = forge_env.service.operations
    task = manager.create(
        kind="pr_check_watch",
        phase="queued",
        cancel_supported=True,
        task_id="task-audit",
    )
    manager.start(task.operation_id)

    forge_env.service.operation_status(task.operation_id)
    status_events = _audit_events(forge_env.root, "operation_status")
    assert len(status_events) == 1
    assert status_events[0]["success"] is True
    assert status_events[0]["details"]["operation_id"] == task.operation_id

    forge_env.service.operation_list(scope="task:task-audit", limit=10)
    list_events = _audit_events(forge_env.root, "operation_list")
    assert len(list_events) == 1
    assert list_events[0]["success"] is True

    forge_env.service.operation_cancel(task.operation_id)
    cancel_events = _audit_events(forge_env.root, "operation_cancel")
    assert len(cancel_events) == 1
    assert cancel_events[0]["success"] is True
    assert cancel_events[0]["details"]["operation_id"] == task.operation_id


def test_operation_status_audits_failure_for_an_unknown_operation_id(
    forge_env: ForgeEnvironment,
) -> None:
    with pytest.raises(RepoForgeError) as exc:
        forge_env.service.operation_status("op-ffffffffffffffffffffffff")
    assert exc.value.code is ErrorCode.OPERATION_NOT_FOUND

    events = _audit_events(forge_env.root, "operation_status")
    assert len(events) == 1
    event = events[0]
    assert event["success"] is False
    assert event["details"]["operation_id"] == "op-ffffffffffffffffffffffff"
    assert event["details"]["error_code"] == ErrorCode.OPERATION_NOT_FOUND.value


def test_operation_cancel_audits_failure_when_the_durable_store_write_fails(
    forge_env: ForgeEnvironment,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = forge_env.service.operations
    task = manager.create(kind="watch", phase="queued", cancel_supported=True)
    manager.start(task.operation_id)

    store = forge_env.service.application.context.operation_store
    assert store is not None

    def fail_save(*_args: object, **_kwargs: object) -> None:
        raise OSError("simulated durable operation store write failure")

    monkeypatch.setattr(store, "save", fail_save)
    with pytest.raises(OSError):
        forge_env.service.operation_cancel(task.operation_id)

    events = _audit_events(forge_env.root, "operation_cancel")
    assert len(events) == 1
    event = events[0]
    assert event["success"] is False
    assert event["details"]["operation_id"] == task.operation_id


def test_in_memory_store_cas_cannot_lose_terminal_transition() -> None:
    store = InMemoryOperationStore()
    pending = _task()
    store.create(pending)
    running = transition_operation(pending, OperationState.RUNNING, now="2026-07-14T00:00:01+00:00")
    store.save(running, expected_updated_at=pending.updated_at)
    succeeded = transition_operation(
        running,
        OperationState.SUCCEEDED,
        now="2026-07-14T00:00:02+00:00",
        result_reference="result-1",
    )
    store.save(succeeded, expected_updated_at=running.updated_at)

    failed = transition_operation(
        running,
        OperationState.FAILED,
        now="2026-07-14T00:00:03+00:00",
        error_code="FAIL",
    )
    with pytest.raises(RepoForgeError) as stale:
        store.save(failed, expected_updated_at=running.updated_at)
    assert stale.value.code is ErrorCode.OPERATION_STALE
    assert store.read(pending.operation_id).state is OperationState.SUCCEEDED  # type: ignore[union-attr]


def test_recovery_with_fixed_clock_does_not_regress_timestamps() -> None:
    clock = FixedClock("2026-07-14T00:00:00+00:00")
    pending = _task(now=clock.now_iso())
    running = transition_operation(pending, OperationState.RUNNING, now=clock.now_iso())
    assert running.updated_at > pending.updated_at


@pytest.mark.anyio
async def test_operation_tools_are_exposed_through_actual_mcp_protocol(
    forge_env: ForgeEnvironment,
) -> None:
    task = forge_env.service.operations.create(
        kind="watch",
        phase="queued",
        cancel_supported=True,
        task_id="task-mcp",
    )
    forge_env.service.operations.start(task.operation_id)
    server = create_server(service=forge_env.service)
    async with create_connected_server_and_client_session(server) as session:
        tools = {item.name: item for item in (await session.list_tools()).tools}
        assert {"operation_status", "operation_list", "operation_cancel"}.issubset(tools)
        assert tools["operation_status"].annotations.readOnlyHint is True
        assert tools["operation_list"].annotations.readOnlyHint is True
        assert tools["operation_cancel"].annotations.readOnlyHint is False
        assert tools["operation_cancel"].annotations.destructiveHint is False
        assert tools["operation_cancel"].annotations.idempotentHint is True

        status = await session.call_tool("operation_status", {"operation_id": task.operation_id})
        assert status.isError is False
        assert status.structuredContent["state"] == "running"
        listed = await session.call_tool(
            "operation_list",
            {"scope": "task:task-mcp", "state": "running", "limit": 20},
        )
        assert listed.isError is False
        assert listed.structuredContent["operations"][0]["operation_id"] == task.operation_id
        cancelled = await session.call_tool(
            "operation_cancel",
            {"operation_id": task.operation_id},
        )
        assert cancelled.isError is False
        assert cancelled.structuredContent["cancellation_requested"] is True


def test_operation_cli_status_list_and_cancel_delegate_to_service(
    forge_env: ForgeEnvironment,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli = importlib.import_module("repoforge.interfaces.cli.main")
    task = forge_env.service.operations.create(
        kind="watch",
        phase="queued",
        cancel_supported=True,
        workspace_id="workspace-cli",
    )
    running = forge_env.service.operations.start(task.operation_id)

    generation = SimpleNamespace(generation=1)
    store = SimpleNamespace(
        active=lambda: generation,
        current=lambda: generation,
        resolved_path=lambda value: forge_env.config_path,
    )
    monkeypatch.setattr(cli, "_ensure_generation", lambda path: store)
    monkeypatch.setattr(cli, "load_config", lambda path: forge_env.service.config)
    monkeypatch.setattr(cli, "CodingService", lambda config: forge_env.service)

    config = str(forge_env.config_path)
    assert cli.main(["--config", config, "operation", "status", task.operation_id]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "running"

    assert (
        cli.main(
            [
                "--config",
                config,
                "operation",
                "list",
                "--scope",
                "workspace:workspace-cli",
                "--state",
                "running",
                "--limit",
                "10",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["operations"][0]["operation_id"] == task.operation_id

    assert (
        cli.main(
            [
                "--config",
                config,
                "operation",
                "cancel",
                task.operation_id,
                "--expected-updated-at",
                running.updated_at,
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["cancellation_requested"] is True


def test_v2_operation_composite_exposes_typed_lifecycle_and_adaptive_polling(
    forge_env: ForgeEnvironment,
) -> None:
    from repoforge.contracts.registry import V2_TOOL_SPECS

    pending = forge_env.service.operations.create(
        kind="index",
        phase="queued",
        cancel_supported=True,
        task_id="task-v2-ops",
        workspace_id="workspace-v2-ops",
    )
    pending_result = forge_env.service.operation(action="get", operation_id=pending.operation_id)
    V2_TOOL_SPECS["operation"].validate_output(pending_result)
    assert pending_result["operation"]["state"] == "pending"
    assert pending_result["operation"]["terminal"] is False
    assert pending_result["operation"]["poll_after_seconds"] > 0
    assert pending_result["operation"]["updated_at"] == pending.updated_at

    running = forge_env.service.operations.start(pending.operation_id)
    cancelled = forge_env.service.operation(
        action="cancel",
        operation_id=pending.operation_id,
        expected_updated_at=running.updated_at,
    )
    assert cancelled["cancellation_requested"] is True
    assert cancelled["operation"]["cancellation_reason"] == "cancellation_requested"
    assert cancelled["operation"]["poll_after_seconds"] <= 1.0

    forge_env.service.operations.cancelled(pending.operation_id)
    terminal = forge_env.service.operation(action="get", operation_id=pending.operation_id)
    assert terminal["operation"]["state"] == "cancelled"
    assert terminal["operation"]["terminal"] is True
    assert terminal["operation"]["poll_after_seconds"] is None
    assert terminal["operation"]["cancellation_reason"] == "cancelled"


def test_v2_operation_composite_lists_with_filters_and_cursor(
    forge_env: ForgeEnvironment,
) -> None:
    first = forge_env.service.operations.create(
        kind="watch",
        phase="queued",
        cancel_supported=True,
        task_id="task-v2-list",
    )
    second = forge_env.service.operations.create(
        kind="watch",
        phase="queued",
        cancel_supported=True,
        task_id="task-v2-list",
    )
    forge_env.service.operations.start(first.operation_id)
    forge_env.service.operations.start(second.operation_id)

    page = forge_env.service.operation(
        action="list",
        scope="task:task-v2-list",
        state="running",
        limit=1,
    )
    assert len(page["operations"]) == 1
    assert page["next_cursor"] is not None
    resumed = forge_env.service.operation(
        action="list",
        scope="task:task-v2-list",
        state="running",
        limit=1,
        cursor=page["next_cursor"],
    )
    assert resumed["operations"][0]["operation_id"] != page["operations"][0]["operation_id"]

    with pytest.raises(ValueError):
        from repoforge.contracts.registry import V2_TOOL_SPECS

        V2_TOOL_SPECS["operation"].validate_input(
            {"action": "list", "operation_id": first.operation_id}
        )


def test_v2_operation_composite_rejects_invalid_direct_actions_with_typed_error(
    forge_env: ForgeEnvironment,
) -> None:
    with pytest.raises(RepoForgeError) as invalid:
        forge_env.service.operation(action="delete")

    assert invalid.value.code is ErrorCode.OPERATION_INVALID
