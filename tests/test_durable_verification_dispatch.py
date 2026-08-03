"""Public verification dispatch through durable operation work."""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import TEST_CONFIG_GENERATION, create_forge_environment

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
    application = build_application(
        load_config(env.config_path), config_generation=TEST_CONFIG_GENERATION
    )
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
    application = build_application(
        load_config(env.config_path), config_generation=TEST_CONFIG_GENERATION
    )
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

    application = build_application(
        load_config(env.config_path), config_generation=TEST_CONFIG_GENERATION
    )

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
        config_generation=TEST_CONFIG_GENERATION,
    )
    service = CodingService(config, application=application)
    workspace_id = service.workspace_create("demo", "durable public verify")["workspace_id"]

    result = service.workspace_verify(
        workspace_id,
        mode="profile",
        profile_name="full",
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
    application = build_application(config, config_generation=TEST_CONFIG_GENERATION)
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


@pytest.mark.parametrize(
    ("mode", "arguments"),
    [
        ("profile", {"profile_name": "full"}),
        (
            "diagnostic",
            {
                "diagnostic_id": "pytest-target",
                "selector": "hello.txt",
                "intent": "tdd_green",
                "expectation": "pass",
            },
        ),
    ],
)
def test_explicit_verification_bypasses_rich_assessment(
    tmp_path,
    monkeypatch,
    mode,
    arguments,
) -> None:
    env = create_forge_environment(tmp_path)
    config = load_config(env.config_path)
    application = build_application(config, config_generation=TEST_CONFIG_GENERATION)
    service = CodingService(config, application=application)
    workspace_id = service.workspace_create("demo", f"minimal {mode} preflight")["workspace_id"]

    def unexpected_assessment(_command):
        raise AssertionError("explicit verification must not collect rich assessment evidence")

    monkeypatch.setattr(service._verify._assessment, "execute", unexpected_assessment)

    result = service.workspace_verify(
        workspace_id,
        mode=mode,
        background=True,
        **arguments,
    )

    assert result["assessment"] is None
    assert result["impact_evidence"] is None
    assert result["recommendations"] == []
    assert result["staleness_warning"] is None
    assert result["operation"]["state"] == "pending"


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
        config_generation=TEST_CONFIG_GENERATION,
    )
    service = CodingService(config, application=application)
    workspace_id = service.workspace_create("demo", "bounded foreground verify")["workspace_id"]

    artifact_path = "artifacts/running-verification.json"
    result = service.workspace_verify(
        workspace_id,
        mode="profile",
        profile_name="full",
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
    application = build_application(config, config_generation=TEST_CONFIG_GENERATION)
    service = CodingService(config, application=application)
    workspace_id = service.workspace_create("demo", "cancel durable verify")["workspace_id"]
    dispatched = service.workspace_verify(
        workspace_id,
        mode="profile",
        profile_name="full",
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


# ---------------- admission ordering and worker-loop survival (#307)


def test_the_operation_record_is_durable_before_the_work_item_exists(tmp_path) -> None:
    """#307: a claimable work item whose operation record cannot be read kills the worker.

    Claiming calls `OperationManager.start`, which READS the operation record, so the
    record must be durable before the item becomes claimable. This asserts the write
    order at the store level -- the moment a work item exists, the record it refers to is
    already readable.
    """
    from repoforge.application.operations.work_admission import DurableWorkAdmission

    env = create_forge_environment(tmp_path)
    application = build_application(
        load_config(env.config_path), config_generation=TEST_CONFIG_GENERATION
    )
    queue = JsonOperationWorkQueue(
        application.context.config.server.state_root,
        application.context.locks,
    )
    observed: list[tuple[str, bool]] = []
    real_create = queue.create

    def recording_create(work):
        # At the instant the item becomes visible, is its operation record readable?
        readable = application.context.operation_store.read(work.operation_id) is not None
        observed.append((work.operation_id, readable))
        return real_create(work)

    queue.create = recording_create  # type: ignore[method-assign]
    admission = DurableWorkAdmission(application.operations, queue)

    operation = admission.admit(
        OperationWorkRequest.profile(
            workspace_id="workspace-1",
            profile_name="full",
            expected_head_sha="a" * 40,
            expected_fingerprint="b" * 64,
            config_generation=12,
        ),
        operation_kind="workspace_run_profile",
    )

    assert observed == [(operation.operation_id, True)]


def test_a_failed_queue_write_terminalizes_the_operation_it_created(tmp_path) -> None:
    """The caller holds the operation_id, so it must resolve to a record saying why
    nothing will run -- never to a pending operation no worker can ever claim."""
    import pytest

    from repoforge.application.operations.work_admission import DurableWorkAdmission
    from repoforge.domain.operation_task import OperationRetryability

    env = create_forge_environment(tmp_path)
    application = build_application(
        load_config(env.config_path), config_generation=TEST_CONFIG_GENERATION
    )
    queue = JsonOperationWorkQueue(
        application.context.config.server.state_root,
        application.context.locks,
    )

    def failing_create(work):
        raise OSError("state root is read-only")

    queue.create = failing_create  # type: ignore[method-assign]
    admission = DurableWorkAdmission(application.operations, queue)

    with pytest.raises(OSError):
        admission.admit(
            OperationWorkRequest.profile(
                workspace_id="workspace-1",
                profile_name="full",
                expected_head_sha="a" * 40,
                expected_fingerprint="b" * 64,
                config_generation=12,
            ),
            operation_kind="workspace_run_profile",
        )

    records = application.operations.list_records(max_records=10).records
    assert len(records) == 1
    failed = records[0]
    assert failed.state is OperationState.FAILED
    assert failed.error_code == "OPERATION_WORK_ADMISSION_FAILED"
    assert failed.retryability is OperationRetryability.MANUAL


def test_recovery_does_not_fail_an_admission_that_is_still_in_flight(tmp_path) -> None:
    """Recovery runs concurrently with admission. "Record present, item absent" is the
    normal state between admission's two writes, so failing it would destroy live work."""
    from repoforge.application.operations.recovery import recover_operation_work

    env = create_forge_environment(tmp_path)
    application = build_application(
        load_config(env.config_path), config_generation=TEST_CONFIG_GENERATION
    )
    queue = JsonOperationWorkQueue(
        application.context.config.server.state_root,
        application.context.locks,
    )
    # Exactly the mid-admission state: the record exists, the work item does not yet.
    operation = application.operations.create(
        kind="workspace_run_profile",
        phase="queued",
        cancel_supported=True,
        now="2026-07-28T10:00:00+00:00",
    )

    report = recover_operation_work(
        application.operations,
        queue,
        now="2026-07-28T10:00:10+00:00",  # 10s later: inside the admission window
    )

    assert report.missing_work == 0
    assert application.operations.status(operation.operation_id).state is OperationState.PENDING


def test_recovery_still_fails_an_admission_that_really_crashed(tmp_path) -> None:
    """Past the window, a queued operation with no work item can never be claimed."""
    from repoforge.application.operations.recovery import recover_operation_work

    env = create_forge_environment(tmp_path)
    application = build_application(
        load_config(env.config_path), config_generation=TEST_CONFIG_GENERATION
    )
    queue = JsonOperationWorkQueue(
        application.context.config.server.state_root,
        application.context.locks,
    )
    operation = application.operations.create(
        kind="workspace_run_profile",
        phase="queued",
        cancel_supported=True,
        now="2026-07-28T10:00:00+00:00",
    )

    report = recover_operation_work(
        application.operations,
        queue,
        now="2026-07-28T10:05:00+00:00",  # well past the window
    )

    assert report.missing_work == 1
    terminal = application.operations.status(operation.operation_id)
    assert terminal.state is OperationState.FAILED
    assert terminal.error_code == "OPERATION_WORK_MISSING"
