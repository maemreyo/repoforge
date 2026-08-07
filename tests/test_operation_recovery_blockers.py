from __future__ import annotations

from datetime import datetime, timedelta

from conftest import TEST_CONFIG_GENERATION, ForgeEnvironment

from repoforge.application.operations.recovery import recover_operation_work
from repoforge.application.operations.work_admission import DurableWorkAdmission
from repoforge.domain.operation_task import OperationState
from repoforge.domain.operation_work import OperationWorkRequest, mark_work_child_started
from repoforge.domain.operation_worker import OperationWorkerBinding


def _expired_started_work(forge_env: ForgeEnvironment):
    application = forge_env.service.application
    queue = application.context.operation_work_queue
    assert queue is not None
    operation = DurableWorkAdmission(application.operations, queue).admit(
        OperationWorkRequest.profile(
            workspace_id="workspace-recovery-blocker",
            profile_name="quick",
            expected_head_sha="a" * 40,
            expected_fingerprint="b" * 64,
            config_generation=TEST_CONFIG_GENERATION,
        ),
        operation_kind="workspace_run_profile",
    )
    now = datetime.fromisoformat(application.context.clock.now_iso())
    claimed_at = now.isoformat()
    lease = (now + timedelta(seconds=1)).isoformat()
    claimed = queue.claim_next(
        owner_id="worker-blocked",
        now=claimed_at,
        lease_expires_at=lease,
        compatible_kinds=frozenset({"profile"}),
        config_generation=TEST_CONFIG_GENERATION,
    )
    assert claimed is not None
    application.operations.start(
        operation.operation_id,
        owner_id="worker-blocked",
        lease_expires_at=lease,
        attempt=claimed.attempt,
        now=claimed_at,
    )
    started = mark_work_child_started(
        claimed,
        owner_id="worker-blocked",
        attempt=claimed.attempt,
        now=(now + timedelta(milliseconds=1)).isoformat(),
    )
    queue.save(started, expected_updated_at=claimed.updated_at)
    return operation.operation_id, started, (now + timedelta(seconds=2)).isoformat()


def test_recovery_reports_missing_binding_as_a_blocker_without_mutating_state(
    forge_env: ForgeEnvironment,
) -> None:
    application = forge_env.service.application
    queue = application.context.operation_work_queue
    bindings = application.context.worker_bindings
    reaper = application.context.reaper
    assert queue is not None and bindings is not None and reaper is not None
    operation_id, started, recovery_now = _expired_started_work(forge_env)

    report = recover_operation_work(
        application.operations,
        queue,
        now=recovery_now,
        expected_config_generation=TEST_CONFIG_GENERATION,
        worker_bindings=bindings,
        reaper=reaper,
    )

    assert report.blocked == 1
    assert report.conflicts == 0
    assert [(item.operation_id, item.code) for item in report.blockers] == [
        (operation_id, "missing_binding")
    ]
    assert application.operations.status(operation_id).state is OperationState.RUNNING
    assert queue.read(operation_id) == started


def test_recovery_reports_owner_mismatch_without_calling_the_reaper(
    forge_env: ForgeEnvironment,
) -> None:
    application = forge_env.service.application
    queue = application.context.operation_work_queue
    bindings = application.context.worker_bindings
    reaper = application.context.reaper
    assert queue is not None and bindings is not None and reaper is not None
    operation_id, started, recovery_now = _expired_started_work(forge_env)
    bindings.put(
        OperationWorkerBinding(
            operation_id=operation_id,
            child_pid=4242,
            child_pgid=4242,
            child_start_token="child-token",
            server_pid=4141,
            server_start_token="server-token",
            created_at=started.updated_at,
            owner_generation=TEST_CONFIG_GENERATION,
            owner_id="different-worker",
            attempt=started.attempt,
        )
    )

    report = recover_operation_work(
        application.operations,
        queue,
        now=recovery_now,
        expected_config_generation=TEST_CONFIG_GENERATION,
        worker_bindings=bindings,
        reaper=reaper,
    )

    assert report.blocked == 1
    assert report.blockers[0].code == "owner_mismatch"
    assert application.operations.status(operation_id).state is OperationState.RUNNING
    assert queue.read(operation_id) == started
    assert bindings.get(operation_id) is not None
