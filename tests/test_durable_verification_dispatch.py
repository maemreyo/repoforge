"""Public verification dispatch through durable operation work."""

from __future__ import annotations

from pathlib import Path

from conftest import create_forge_environment

from repoforge.adapters.persistence.json_operation_work_queue import (
    JsonOperationWorkQueue,
)
from repoforge.application.service import CodingService
from repoforge.bootstrap import AdapterOverrides, build_application
from repoforge.config import load_config
from repoforge.domain.operation_task import OperationState
from repoforge.domain.operation_work import OperationWorkRequest, OperationWorkState
from repoforge.testing.fakes import ManualBackgroundTaskRunner


def test_admission_persists_queued_phase_without_marking_running(tmp_path) -> None:
    """Catch admission that reports running before any worker owns the job."""
    from repoforge.application.operations.work_admission import DurableWorkAdmission

    env = create_forge_environment(tmp_path)
    application = build_application(load_config(env.config_path))
    queue = JsonOperationWorkQueue(
        application.context.config.server.state_root,
        application.context.locks,
    )
    admission = DurableWorkAdmission(application.operations, queue)
    request = OperationWorkRequest.profile(
        workspace_id="workspace-1",
        profile_name="full",
        expected_head_sha="a" * 40,
        expected_fingerprint="b" * 64,
        config_generation=12,
    )

    operation = admission.admit(
        request,
        operation_kind="workspace_run_profile",
    )

    assert operation.state is OperationState.PENDING
    assert operation.phase == "queued"
    work = queue.read(operation.operation_id)
    assert work is not None
    assert work.state is OperationWorkState.QUEUED
    assert work.request == request


def test_pending_cancel_terminalizes_and_deletes_work(tmp_path) -> None:
    """Catch cancelled queued work remaining available for a future worker claim."""
    from repoforge.application.operations.work_admission import DurableWorkAdmission

    env = create_forge_environment(tmp_path)
    application = build_application(load_config(env.config_path))
    queue = JsonOperationWorkQueue(
        application.context.config.server.state_root,
        application.context.locks,
    )
    admission = DurableWorkAdmission(application.operations, queue)
    operation = admission.admit(
        OperationWorkRequest.profile(
            workspace_id="workspace-1",
            profile_name="full",
            expected_head_sha="c" * 40,
            expected_fingerprint="d" * 64,
            config_generation=12,
        ),
        operation_kind="workspace_run_profile",
    )

    cancelled = admission.cancel(operation.operation_id)

    assert cancelled.state is OperationState.CANCELLED
    assert cancelled.phase == "cancelled"
    assert queue.read(operation.operation_id) is None


def test_bootstrap_wires_durable_work_queue(tmp_path) -> None:
    """Catch production composition that leaves durable admission unavailable."""
    env = create_forge_environment(tmp_path)

    application = build_application(load_config(env.config_path))

    queue = application.context.operation_work_queue
    assert isinstance(queue, JsonOperationWorkQueue)


def test_public_background_profile_admits_queued_work_without_daemon_closure(tmp_path) -> None:
    """Public dispatch persists work and never hands a repository command to a request thread."""
    env = create_forge_environment(tmp_path)
    config = load_config(env.config_path)
    legacy_background = ManualBackgroundTaskRunner()
    application = build_application(
        config,
        overrides=AdapterOverrides(background_tasks=legacy_background),
    )
    service = CodingService(config, application=application)
    workspace_id = service.workspace_create("demo", "durable public verify")["workspace_id"]

    result = service.workspace_verify(
        workspace_id,
        mode="profile",
        profile_name="quick",
        background=True,
    )

    operation = result["operation"]
    assert operation is not None
    assert operation["state"] == "pending"
    assert operation["phase"] == "queued"
    queue = application.context.operation_work_queue
    assert queue is not None
    work = queue.read(operation["operation_id"])
    assert work is not None
    assert work.state is OperationWorkState.QUEUED
    assert work.request.kind == "profile"
    assert legacy_background.keys == ()


def test_public_background_diagnostic_is_durable_and_preserves_selectors(tmp_path) -> None:
    env = create_forge_environment(tmp_path)
    config = load_config(env.config_path)
    application = build_application(config)
    service = CodingService(config, application=application)
    workspace_id = service.workspace_create("demo", "durable diagnostic")["workspace_id"]

    result = service.workspace_verify(
        workspace_id,
        mode="diagnostic",
        diagnostic_id="pytest-target",
        selector="hello.txt",
        background=True,
        intent="tdd_green",
        expectation="pass",
    )

    operation = result["operation"]
    assert operation is not None
    assert operation["kind"] == "workspace_run_diagnostic"
    assert operation["phase"] == "queued"
    queue = application.context.operation_work_queue
    assert queue is not None
    work = queue.read(operation["operation_id"])
    assert work is not None
    assert work.request.kind == "diagnostic"
    assert work.request.diagnostic_id == "pytest-target"
    assert work.request.selector == ("hello.txt",)
    assert work.request.intent == "tdd_green"
    assert work.request.expectation == "pass"


def test_foreground_profile_only_bounded_waits_on_durable_operation(
    tmp_path,
    monkeypatch,
) -> None:
    """A foreground request never executes the profile when no worker has claimed it."""
    import repoforge.application.workspace.verify as verify_module

    monkeypatch.setattr(verify_module, "_FOREGROUND_WAIT_SECONDS", 0.01)
    monkeypatch.setattr(verify_module, "_FOREGROUND_POLL_SECONDS", 0.001)
    env = create_forge_environment(tmp_path)
    config = load_config(env.config_path)
    legacy_background = ManualBackgroundTaskRunner()
    application = build_application(
        config,
        overrides=AdapterOverrides(background_tasks=legacy_background),
    )
    service = CodingService(config, application=application)
    workspace_id = service.workspace_create("demo", "bounded foreground verify")["workspace_id"]

    artifact_path = "artifacts/running-verification.json"
    result = service.workspace_verify(
        workspace_id,
        mode="profile",
        profile_name="quick",
        background=False,
        artifact_output_path=artifact_path,
    )

    operation = result["operation"]
    assert result["outcome"] == "running"
    assert operation is not None
    assert operation["state"] == "pending"
    assert operation["phase"] == "queued"
    assert result["output_artifact_status"] == "source_unavailable"
    workspace_path = Path(service.workspace_status(workspace_id)["path"])
    assert not workspace_path.joinpath(artifact_path).exists()
    queue = application.context.operation_work_queue
    assert queue is not None
    assert queue.read(operation["operation_id"]) is not None
    assert legacy_background.keys == ()


def test_public_cancel_terminalizes_queued_work_and_is_idempotent(tmp_path) -> None:
    """The public operation API must make queued verification unclaimable immediately."""
    env = create_forge_environment(tmp_path)
    config = load_config(env.config_path)
    application = build_application(config)
    service = CodingService(config, application=application)
    workspace_id = service.workspace_create("demo", "cancel durable verify")["workspace_id"]
    dispatched = service.workspace_verify(
        workspace_id,
        mode="profile",
        profile_name="quick",
        background=True,
    )
    operation_id = dispatched["operation"]["operation_id"]

    first = service.operation_cancel(operation_id)
    second = service.operation_cancel(operation_id)

    assert first["operation"]["state"] == "cancelled"
    assert second["operation"]["state"] == "cancelled"
    assert second["already_terminal"] is True
    queue = application.context.operation_work_queue
    assert queue is not None
    assert queue.read(operation_id) is None
