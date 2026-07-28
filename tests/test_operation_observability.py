"""Truthful public observability for durable operations."""

from __future__ import annotations

from datetime import datetime, timedelta

from conftest import ForgeEnvironment

from repoforge.application.service import CodingService
from repoforge.config import load_config
from repoforge.contracts.common import OperationEvidence


def _lease_after(now: str, seconds: int = 90) -> str:
    return (datetime.fromisoformat(now) + timedelta(seconds=seconds)).isoformat()


def test_running_operation_reports_persisted_attempt_and_heartbeat(
    forge_env: ForgeEnvironment,
) -> None:
    service = forge_env.service
    manager = service.operations
    now = service.application.context.clock.now_iso()
    task = manager.create(kind="watch", phase="queued", cancel_supported=True, now=now)
    running = manager.start(
        task.operation_id,
        owner_id="worker-observe",
        lease_expires_at=_lease_after(now),
        attempt=2,
        now=now,
    )

    status = service.operation_status(task.operation_id)
    assert status["attempt"] == 2
    assert status["heartbeat_at"] == running.updated_at
    # Age is derived at read time from the persisted heartbeat, never stored, so it
    # grows with the wall clock instead of staying pinned at the persisted instant.
    assert 0.0 <= status["heartbeat_age_seconds"] < 60.0
    assert status["evidence_complete"] is False

    evidence = service.operation("get", operation_id=task.operation_id)["operation"]
    assert evidence is not None
    assert evidence["attempt"] == 2
    assert evidence["heartbeat_at"] == running.updated_at
    assert 0.0 <= evidence["heartbeat_age_seconds"] < 60.0
    assert evidence["evidence_complete"] is False
    OperationEvidence.model_validate(evidence)


def test_terminal_attempt_survives_sidecar_cleanup_and_process_restart(
    forge_env: ForgeEnvironment,
) -> None:
    service = forge_env.service
    manager = service.operations
    ctx = service.application.context
    assert ctx.operation_result_store is not None
    now = ctx.clock.now_iso()
    task = manager.create(kind="watch", phase="queued", cancel_supported=True, now=now)
    manager.start(
        task.operation_id,
        owner_id="worker-observe",
        lease_expires_at=_lease_after(now),
        attempt=3,
        now=now,
    )
    ctx.operation_result_store.save(task.operation_id, {"outcome": "verified"})
    manager.succeed(
        task.operation_id,
        result_reference=f"operation-result:{task.operation_id}",
        owner_id="worker-observe",
        now=now,
    )

    terminal = service.operation_status(task.operation_id)
    assert terminal["attempt"] == 3
    assert terminal["heartbeat_at"] is None
    assert terminal["heartbeat_age_seconds"] is None
    assert terminal["evidence_complete"] is True

    restarted = CodingService(load_config(forge_env.config_path))
    recovered = restarted.operation_status(task.operation_id)
    assert recovered["attempt"] == 3
    assert recovered["evidence_complete"] is True


def test_missing_success_payload_is_explicitly_evidence_incomplete(
    forge_env: ForgeEnvironment,
) -> None:
    service = forge_env.service
    manager = service.operations
    now = service.application.context.clock.now_iso()
    task = manager.create(kind="watch", phase="queued", cancel_supported=False, now=now)
    manager.start(task.operation_id, now=now)
    manager.succeed(
        task.operation_id,
        result_reference=f"operation-result:{task.operation_id}",
        now=now,
    )

    status = service.operation_status(task.operation_id)
    assert status["result_reference_status"] == "missing"
    assert status["evidence_complete"] is False


def test_terminal_error_record_is_complete_without_result_payload(
    forge_env: ForgeEnvironment,
) -> None:
    service = forge_env.service
    manager = service.operations
    now = service.application.context.clock.now_iso()
    task = manager.create(kind="watch", phase="queued", cancel_supported=False, now=now)
    manager.start(task.operation_id, now=now)
    manager.fail(task.operation_id, error_code="COMMAND_FAILED", now=now)

    status = service.operation_status(task.operation_id)
    assert status["attempt"] == 0
    assert status["evidence_complete"] is True
    listed = service.operation_list(state="failed")
    item = next(
        record for record in listed["operations"] if record["operation_id"] == task.operation_id
    )
    assert item["evidence_complete"] is True
