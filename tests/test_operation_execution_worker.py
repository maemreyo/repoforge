"""Durable execution-worker lifecycle and typed handler coverage."""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta

import pytest
from conftest import create_forge_environment

from repoforge.application.fingerprint_cache import read_fingerprint
from repoforge.application.service import CodingService
from repoforge.application.workspace.run_adhoc import WorkspaceAdhocRunner
from repoforge.application.workspace.run_profile import WorkspaceProfileRunner
from repoforge.bootstrap import build_application
from repoforge.config import load_config
from repoforge.domain.errors import ErrorCode, RepoForgeError
from repoforge.domain.operation_work import OperationWorkRequest, new_work_item
from repoforge.ports.cancellation import CancellationToken


def test_profile_handler_reconstructs_exact_command_without_recursive_enqueue(tmp_path) -> None:
    """A claimed profile runs synchronously and never creates another work record."""
    from repoforge.application.operations.work_executor import VerificationWorkHandlers

    env = create_forge_environment(tmp_path)
    config = load_config(env.config_path)
    application = build_application(config)
    service = CodingService(config, application=application)
    created = service.workspace_create("demo", "durable profile handler")
    workspace_id = created["workspace_id"]
    _record, _repo, workspace_path = application.context.workspace(workspace_id)
    fingerprint = read_fingerprint(
        application.context.fingerprint_cache,
        workspace_id,
        application.context.git,
        workspace_path,
    ).fingerprint
    head_sha = application.context.git.head_sha(workspace_path)
    request = OperationWorkRequest.profile(
        workspace_id=workspace_id,
        profile_name="quick",
        expected_head_sha=head_sha,
        expected_fingerprint=fingerprint,
        config_generation=12,
    )
    item = new_work_item(
        operation_id="op-" + "1" * 24, request=request, now="2026-07-27T01:00:00+00:00"
    )
    queue = application.context.operation_work_queue
    assert queue is not None
    queue.create(item)
    claimed = queue.claim_next(
        owner_id="worker-test",
        now="2026-07-27T01:00:01+00:00",
        lease_expires_at="2026-07-27T01:01:31+00:00",
        compatible_kinds=frozenset({"profile"}),
    )
    assert claimed is not None
    progress: list[tuple[object, ...]] = []
    handlers = VerificationWorkHandlers(
        WorkspaceProfileRunner(application.context),
        WorkspaceAdhocRunner(application.context),
    )

    result = handlers.execute(
        claimed,
        cancellation_token=CancellationToken(),
        progress=lambda *event: progress.append(event),
    )

    assert result.profile == "quick"
    assert queue.list_records(max_records=100).records == (claimed,)
    assert progress


def test_adhoc_handler_reconstructs_reviewed_command_without_recursive_enqueue(tmp_path) -> None:
    """A claimed ad-hoc request preserves argv and executes without queue recursion."""
    from repoforge.application.operations.work_executor import VerificationWorkHandlers

    env = create_forge_environment(
        tmp_path,
        execution_mode="relaxed",
        adhoc_runners=("python3",),
    )
    config = load_config(env.config_path)
    application = build_application(config)
    service = CodingService(config, application=application)
    created = service.workspace_create("demo", "durable adhoc handler")
    workspace_id = created["workspace_id"]
    _record, _repo, workspace_path = application.context.workspace(workspace_id)
    fingerprint = read_fingerprint(
        application.context.fingerprint_cache,
        workspace_id,
        application.context.git,
        workspace_path,
    ).fingerprint
    request = OperationWorkRequest.adhoc(
        workspace_id=workspace_id,
        argv=("python3", "-c", "print('durable adhoc')"),
        working_directory=None,
        mutability="read_only",
        expected_head_sha=application.context.git.head_sha(workspace_path),
        expected_fingerprint=fingerprint,
        config_generation=12,
    )
    item = new_work_item(
        operation_id="op-" + "2" * 24,
        request=request,
        now="2026-07-27T01:00:00+00:00",
    )
    queue = application.context.operation_work_queue
    assert queue is not None
    queue.create(item)
    claimed = queue.claim_next(
        owner_id="worker-test",
        now="2026-07-27T01:00:01+00:00",
        lease_expires_at="2026-07-27T01:01:31+00:00",
        compatible_kinds=frozenset({"adhoc"}),
    )
    assert claimed is not None
    progress: list[tuple[object, ...]] = []
    handlers = VerificationWorkHandlers(
        WorkspaceProfileRunner(application.context),
        WorkspaceAdhocRunner(application.context),
    )

    result = handlers.execute(
        claimed,
        cancellation_token=CancellationToken(),
        progress=lambda *event: progress.append(event),
    )

    assert result.argv == ["python3", "-c", "print('durable adhoc')"]
    assert result.stdout.strip() == "durable adhoc"
    assert queue.list_records(max_records=100).records == (claimed,)
    assert progress[0][0] == "running"


def test_worker_claims_once_publishes_progress_and_terminal_result(tmp_path) -> None:
    """A worker owns the complete queued -> running -> succeeded lifecycle."""
    from repoforge.application.operations.work_admission import DurableWorkAdmission
    from repoforge.application.operations.work_loop import OperationWorkLoop
    from repoforge.domain.operation_task import OperationState

    class SuccessfulHandlers:
        def execute(self, item, *, cancellation_token, progress):
            progress("running", 1, 1, "commands", "completed durable test work")
            return {"workspace_id": item.request.workspace_id, "ok": True}

    env = create_forge_environment(tmp_path)
    application = build_application(load_config(env.config_path))
    queue = application.context.operation_work_queue
    result_store = application.context.operation_result_store
    assert queue is not None
    assert result_store is not None
    operation = DurableWorkAdmission(application.operations, queue).admit(
        OperationWorkRequest.profile(
            workspace_id="workspace-1",
            profile_name="quick",
            expected_head_sha="a" * 40,
            expected_fingerprint="b" * 64,
            config_generation=12,
        ),
        operation_kind="workspace_run_profile",
    )
    worker = OperationWorkLoop(
        application.context,
        application.operations,
        SuccessfulHandlers(),
        owner_id="worker-test",
    )

    assert worker.run_once() is True

    terminal = application.operations.status(operation.operation_id)
    assert terminal.state is OperationState.SUCCEEDED
    assert terminal.phase == "succeeded"
    assert terminal.progress_current == 1
    assert terminal.owner_id is None
    assert result_store.read(operation.operation_id) == {
        "workspace_id": "workspace-1",
        "ok": True,
    }
    assert queue.read(operation.operation_id) is None
    assert worker.run_once() is False


def test_expired_claim_without_started_child_is_requeued(tmp_path) -> None:
    """A crash before process start is safe to retry and returns the operation to queued."""
    from repoforge.application.operations.recovery import recover_operation_work
    from repoforge.application.operations.work_admission import DurableWorkAdmission

    env = create_forge_environment(tmp_path)
    application = build_application(load_config(env.config_path))
    queue = application.context.operation_work_queue
    assert queue is not None
    operation = DurableWorkAdmission(application.operations, queue).admit(
        OperationWorkRequest.profile(
            workspace_id="workspace-1",
            profile_name="quick",
            expected_head_sha="c" * 40,
            expected_fingerprint="d" * 64,
            config_generation=12,
        ),
        operation_kind="workspace_run_profile",
    )
    now = datetime.fromisoformat(application.context.clock.now_iso())
    claimed = queue.claim_next(
        owner_id="worker-dead",
        now=now.isoformat(),
        lease_expires_at=(now + timedelta(seconds=1)).isoformat(),
        compatible_kinds=frozenset({"profile"}),
    )
    assert claimed is not None

    report = recover_operation_work(
        application.operations,
        queue,
        now=(now + timedelta(seconds=2)).isoformat(),
    )

    assert report.requeued == 1
    recovered = queue.read(operation.operation_id)
    assert recovered is not None
    assert recovered.state.value == "queued"
    assert recovered.attempt == 1
    assert application.operations.status(operation.operation_id).phase == "queued"


def test_expired_started_claim_without_binding_is_preserved_fail_closed(tmp_path) -> None:
    """Unknown child outcome must retain its sidecar and block takeover."""
    from repoforge.application.operations.recovery import recover_operation_work
    from repoforge.application.operations.work_admission import DurableWorkAdmission
    from repoforge.domain.operation_task import OperationState
    from repoforge.domain.operation_work import mark_work_child_started

    env = create_forge_environment(tmp_path)
    application = build_application(load_config(env.config_path))
    queue = application.context.operation_work_queue
    assert queue is not None
    operation = DurableWorkAdmission(application.operations, queue).admit(
        OperationWorkRequest.profile(
            workspace_id="workspace-1",
            profile_name="quick",
            expected_head_sha="e" * 40,
            expected_fingerprint="f" * 64,
            config_generation=12,
        ),
        operation_kind="workspace_run_profile",
    )
    now = datetime.fromisoformat(application.context.clock.now_iso())
    lease = (now + timedelta(seconds=1)).isoformat()
    claimed = queue.claim_next(
        owner_id="worker-dead",
        now=now.isoformat(),
        lease_expires_at=lease,
        compatible_kinds=frozenset({"profile"}),
    )
    assert claimed is not None
    started = mark_work_child_started(
        claimed,
        owner_id="worker-dead",
        attempt=claimed.attempt,
        now=(now + timedelta(milliseconds=1)).isoformat(),
    )
    queue.save(started, expected_updated_at=claimed.updated_at)
    application.operations.start(
        operation.operation_id,
        owner_id="worker-dead",
        lease_expires_at=lease,
        now=(now + timedelta(milliseconds=1)).isoformat(),
    )

    report = recover_operation_work(
        application.operations,
        queue,
        now=(now + timedelta(seconds=2)).isoformat(),
    )

    assert report.conflicts == 0
    assert report.blocked == 1
    assert report.blockers[0].code == "containment_unconfigured"
    assert queue.read(operation.operation_id) is not None
    assert application.operations.status(operation.operation_id).state is OperationState.RUNNING


def test_worker_polls_durable_cancellation_before_publishing_success(tmp_path) -> None:
    """A running worker must observe persisted cancellation and never publish success."""
    from repoforge.application.operations.work_admission import DurableWorkAdmission
    from repoforge.application.operations.work_loop import OperationWorkLoop
    from repoforge.domain.operation_task import OperationState

    class CooperativeHandlers:
        def __init__(self) -> None:
            self.started = threading.Event()

        def execute(self, item, *, cancellation_token, progress):
            self.started.set()
            deadline = time.monotonic() + 0.5
            while not cancellation_token.is_cancelled() and time.monotonic() < deadline:
                time.sleep(0.005)
            return {"cancel_observed": cancellation_token.is_cancelled()}

    env = create_forge_environment(tmp_path)
    application = build_application(load_config(env.config_path))
    queue = application.context.operation_work_queue
    result_store = application.context.operation_result_store
    assert queue is not None
    assert result_store is not None
    operation = DurableWorkAdmission(application.operations, queue).admit(
        OperationWorkRequest.profile(
            workspace_id="workspace-1",
            profile_name="quick",
            expected_head_sha="1" * 40,
            expected_fingerprint="2" * 64,
            config_generation=12,
        ),
        operation_kind="workspace_run_profile",
    )
    handlers = CooperativeHandlers()
    worker = OperationWorkLoop(
        application.context,
        application.operations,
        handlers,
        owner_id="worker-cancel",
        heartbeat_interval_seconds=0.01,
    )
    worker_thread = threading.Thread(target=worker.run_once, daemon=True)

    worker_thread.start()
    assert handlers.started.wait(timeout=1)
    application.operations.request_cancel(operation.operation_id)
    worker_thread.join(timeout=2)

    assert not worker_thread.is_alive()
    terminal = application.operations.status(operation.operation_id)
    assert terminal.state is OperationState.CANCELLED
    assert result_store.read(operation.operation_id) is None
    assert queue.read(operation.operation_id) is None


def test_worker_renews_claims_without_handler_progress(tmp_path) -> None:
    """Opaque commands must retain ownership even when handlers emit no progress."""
    from repoforge.application.operations.work_admission import DurableWorkAdmission
    from repoforge.application.operations.work_loop import OperationWorkLoop

    env = create_forge_environment(tmp_path)
    application = build_application(load_config(env.config_path))
    queue = application.context.operation_work_queue
    result_store = application.context.operation_result_store
    assert queue is not None
    assert result_store is not None
    operation = DurableWorkAdmission(application.operations, queue).admit(
        OperationWorkRequest.adhoc(
            workspace_id="workspace-1",
            argv=("python3", "-c", "print('opaque')"),
            working_directory=".",
            mutability="read_only",
            expected_head_sha="3" * 40,
            expected_fingerprint="4" * 64,
            config_generation=12,
        ),
        operation_kind="workspace_run_adhoc",
    )

    class OpaqueHandlers:
        def execute(self, item, *, cancellation_token, progress):
            initial_task = application.operations.status(item.operation_id)
            initial_work = queue.read(item.operation_id)
            assert initial_work is not None
            deadline = time.monotonic() + 0.5
            while time.monotonic() < deadline:
                task = application.operations.status(item.operation_id)
                work = queue.read(item.operation_id)
                if (
                    work is not None
                    and task.updated_at != initial_task.updated_at
                    and work.updated_at != initial_work.updated_at
                ):
                    return {"heartbeat_observed": True}
                time.sleep(0.005)
            raise AssertionError("generic worker heartbeat did not renew both durable claims")

    worker = OperationWorkLoop(
        application.context,
        application.operations,
        OpaqueHandlers(),
        owner_id="worker-heartbeat",
        heartbeat_interval_seconds=0.01,
    )

    assert worker.run_once() is True
    assert result_store.read(operation.operation_id) == {"heartbeat_observed": True}


def test_recovery_terminalizes_queued_work_from_a_stale_generation(tmp_path) -> None:
    """Generation handoff must fail stale queued work instead of stranding it forever."""
    from repoforge.application.operations.recovery import recover_operation_work
    from repoforge.application.operations.work_admission import DurableWorkAdmission
    from repoforge.domain.operation_task import OperationState

    env = create_forge_environment(tmp_path)
    application = build_application(load_config(env.config_path))
    queue = application.context.operation_work_queue
    assert queue is not None
    operation = DurableWorkAdmission(application.operations, queue).admit(
        OperationWorkRequest.profile(
            workspace_id="workspace-1",
            profile_name="quick",
            expected_head_sha="5" * 40,
            expected_fingerprint="6" * 64,
            config_generation=11,
        ),
        operation_kind="workspace_run_profile",
    )

    report = recover_operation_work(
        application.operations,
        queue,
        now=application.context.clock.now_iso(),
        expected_config_generation=12,
    )

    terminal = application.operations.status(operation.operation_id)
    assert report.stale_generation == 1
    assert terminal.state is OperationState.FAILED
    assert terminal.error_code == "OPERATION_GENERATION_STALE"
    assert queue.read(operation.operation_id) is None


def test_queued_operation_without_its_work_sidecar_fails_closed(tmp_path) -> None:
    """A crash between the two admission writes must not strand an unclaimable job.

    Recovery waits out a bounded admission window first (#307): "record present, item
    absent" is also the normal state for the microseconds between admission's two writes,
    so this asserts the state PAST that window -- which is where every real recovery pass
    sees a crashed admission, since recovery runs on a 30s interval.
    """
    from datetime import datetime, timedelta

    from repoforge.application.operations.recovery import recover_operation_work
    from repoforge.application.operations.work_admission import DurableWorkAdmission
    from repoforge.domain.operation_task import OperationState

    env = create_forge_environment(tmp_path)
    application = build_application(load_config(env.config_path))
    queue = application.context.operation_work_queue
    assert queue is not None
    operation = DurableWorkAdmission(application.operations, queue).admit(
        OperationWorkRequest.profile(
            workspace_id="workspace-1",
            profile_name="quick",
            expected_head_sha="7" * 40,
            expected_fingerprint="8" * 64,
            config_generation=11,
        ),
        operation_kind="workspace_run_profile",
    )
    queue.delete(operation.operation_id)

    report = recover_operation_work(
        application.operations,
        queue,
        now=(
            datetime.fromisoformat(application.context.clock.now_iso()) + timedelta(minutes=5)
        ).isoformat(),
    )

    terminal = application.operations.status(operation.operation_id)
    assert report.missing_work == 1
    assert terminal.state is OperationState.FAILED
    assert terminal.error_code == "OPERATION_WORK_MISSING"


def test_running_operation_without_a_work_sidecar_is_left_to_liveness_recovery(tmp_path) -> None:
    """The missing-sidecar rule must not seize an execution it cannot judge.

    A running record proves a worker claimed the job, so its outcome belongs to
    lease-expiry and worker-liveness recovery, which carry the exact evidence.
    An in-process background run legitimately has no sidecar at all.
    """
    from repoforge.application.operations.recovery import recover_operation_work
    from repoforge.domain.operation_task import OperationState

    env = create_forge_environment(tmp_path)
    application = build_application(load_config(env.config_path))
    queue = application.context.operation_work_queue
    assert queue is not None
    now = application.context.clock.now_iso()
    task = application.operations.create(
        kind="workspace_run_profile",
        phase="queued",
        cancel_supported=True,
        workspace_id="workspace-1",
        now=now,
    )
    application.operations.start(
        task.operation_id,
        owner_id="worker-in-process",
        lease_expires_at=(datetime.fromisoformat(now) + timedelta(seconds=90)).isoformat(),
        now=now,
    )

    report = recover_operation_work(
        application.operations,
        queue,
        now=application.context.clock.now_iso(),
    )

    assert report.missing_work == 0
    assert application.operations.status(task.operation_id).state is OperationState.RUNNING


def test_worker_periodically_recovers_a_claim_that_expires_after_restart(tmp_path) -> None:
    """A pre-expiry restart must eventually reclaim work without another process restart."""
    from repoforge.application.operations.work_admission import DurableWorkAdmission
    from repoforge.application.operations.work_loop import OperationWorkLoop
    from repoforge.domain.operation_task import OperationState

    env = create_forge_environment(tmp_path)
    application = build_application(load_config(env.config_path))
    queue = application.context.operation_work_queue
    assert queue is not None
    generation = application.context.config_generation or 12
    operation = DurableWorkAdmission(application.operations, queue).admit(
        OperationWorkRequest.profile(
            workspace_id="workspace-1",
            profile_name="quick",
            expected_head_sha="7" * 40,
            expected_fingerprint="8" * 64,
            config_generation=generation,
        ),
        operation_kind="workspace_run_profile",
    )
    claimed_at = datetime.fromisoformat(application.context.clock.now_iso())
    lease = (claimed_at + timedelta(seconds=0.05)).isoformat()
    claimed = queue.claim_next(
        owner_id="worker-before-restart",
        now=claimed_at.isoformat(),
        lease_expires_at=lease,
        compatible_kinds=frozenset({"profile"}),
        config_generation=generation,
    )
    assert claimed is not None
    application.operations.start(
        operation.operation_id,
        owner_id="worker-before-restart",
        lease_expires_at=lease,
        attempt=claimed.attempt,
        now=claimed_at.isoformat(),
    )
    stop = threading.Event()

    class RecoveredHandlers:
        def execute(self, item, *, cancellation_token, progress):
            stop.set()
            return {"attempt": item.attempt}

    worker = OperationWorkLoop(
        application.context,
        application.operations,
        RecoveredHandlers(),
        owner_id="worker-after-restart",
        idle_poll_seconds=0.005,
        heartbeat_interval_seconds=0.01,
        recovery_interval_seconds=0.01,
    )
    worker_thread = threading.Thread(
        target=worker.run_until_stopped,
        args=(stop,),
        daemon=True,
    )

    worker_thread.start()
    worker_thread.join(timeout=2)

    assert not worker_thread.is_alive()
    terminal = application.operations.status(operation.operation_id)
    assert terminal.state is OperationState.SUCCEEDED
    assert terminal.attempt == 2


def test_public_cancel_reaps_a_real_bound_child_across_worker_context(tmp_path) -> None:
    """Durable cancellation must terminate the bound OS group without local token access."""
    from repoforge.application.operations.work_admission import DurableWorkAdmission
    from repoforge.application.operations.work_loop import OperationWorkLoop
    from repoforge.domain.operation_task import OperationState

    env = create_forge_environment(tmp_path)
    config = load_config(env.config_path)
    application = build_application(config)
    service = CodingService(config, application=application)
    queue = application.context.operation_work_queue
    bindings = application.context.worker_bindings
    reaper = application.context.reaper
    assert queue is not None
    assert bindings is not None
    assert reaper is not None
    generation = application.context.config_generation or 12
    operation = DurableWorkAdmission(application.operations, queue).admit(
        OperationWorkRequest.profile(
            workspace_id="workspace-1",
            profile_name="quick",
            expected_head_sha="9" * 40,
            expected_fingerprint="a" * 64,
            config_generation=generation,
        ),
        operation_kind="workspace_run_profile",
    )

    class RealChildHandlers:
        def execute(self, item, *, cancellation_token, progress):
            application.context.commands.run(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                cwd=tmp_path,
                timeout=30,
                cancel_token=cancellation_token,
            )
            return {"unexpected": "completed"}

    worker = OperationWorkLoop(
        application.context,
        application.operations,
        RealChildHandlers(),
        owner_id="worker-real-cancel",
        heartbeat_interval_seconds=60,
    )
    worker_thread = threading.Thread(target=worker.run_once, daemon=True)
    worker_thread.start()
    deadline = time.monotonic() + 2
    binding = bindings.get(operation.operation_id)
    while binding is None and time.monotonic() < deadline:
        time.sleep(0.01)
        binding = bindings.get(operation.operation_id)
    assert binding is not None

    service.operation_cancel(operation.operation_id)
    worker_thread.join(timeout=3)

    assert not worker_thread.is_alive()
    assert reaper.read_start_token(binding.child_pid) is None
    assert bindings.get(operation.operation_id) is None
    assert application.operations.status(operation.operation_id).state is OperationState.CANCELLED


def test_generation_recovery_reaps_real_child_before_deleting_sidecar(tmp_path) -> None:
    """A new generation must contain the old child before orphaning its operation."""
    from repoforge.application.operations.recovery import recover_operation_work
    from repoforge.application.operations.work_admission import DurableWorkAdmission
    from repoforge.domain.operation_task import OperationState
    from repoforge.domain.operation_work import mark_work_child_started
    from repoforge.domain.operation_worker import OperationWorkerBinding

    env = create_forge_environment(tmp_path)
    application = build_application(load_config(env.config_path))
    queue = application.context.operation_work_queue
    bindings = application.context.worker_bindings
    reaper = application.context.reaper
    assert queue is not None
    assert bindings is not None
    assert reaper is not None
    operation = DurableWorkAdmission(application.operations, queue).admit(
        OperationWorkRequest.profile(
            workspace_id="workspace-1",
            profile_name="quick",
            expected_head_sha="b" * 40,
            expected_fingerprint="c" * 64,
            config_generation=11,
        ),
        operation_kind="workspace_run_profile",
    )
    now = datetime.fromisoformat(application.context.clock.now_iso())
    lease = (now + timedelta(seconds=90)).isoformat()
    claimed = queue.claim_next(
        owner_id="worker-old-generation",
        now=now.isoformat(),
        lease_expires_at=lease,
        compatible_kinds=frozenset({"profile"}),
        config_generation=11,
    )
    assert claimed is not None
    application.operations.start(
        operation.operation_id,
        owner_id="worker-old-generation",
        lease_expires_at=lease,
        attempt=claimed.attempt,
        now=now.isoformat(),
    )
    started = mark_work_child_started(
        claimed,
        owner_id="worker-old-generation",
        attempt=claimed.attempt,
        now=(now + timedelta(milliseconds=1)).isoformat(),
    )
    queue.save(started, expected_updated_at=claimed.updated_at)
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        start_new_session=True,
    )
    try:
        child_token = reaper.read_start_token(process.pid)
        server_token = reaper.read_start_token(os.getpid())
        assert child_token is not None
        assert server_token is not None
        bindings.put(
            OperationWorkerBinding(
                operation_id=operation.operation_id,
                child_pid=process.pid,
                child_pgid=process.pid,
                child_start_token=child_token,
                server_pid=os.getpid(),
                server_start_token=server_token,
                created_at=application.context.clock.now_iso(),
                owner_generation=11,
                owner_id="worker-old-generation",
                attempt=claimed.attempt,
            )
        )

        report = recover_operation_work(
            application.operations,
            queue,
            now=application.context.clock.now_iso(),
            expected_config_generation=12,
            worker_bindings=bindings,
            reaper=reaper,
        )
        process.wait(timeout=3)

        terminal = application.operations.status(operation.operation_id)
        assert report.stale_generation == 1
        assert terminal.state is OperationState.ORPHANED
        assert terminal.error_code == "OPERATION_GENERATION_STALE"
        assert bindings.get(operation.operation_id) is None
        assert queue.read(operation.operation_id) is None
    finally:
        if process.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                process.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=1)
            if process.poll() is None:
                process.kill()


def test_public_cancel_terminalizes_after_dead_owner_and_preserves_proof(tmp_path) -> None:
    """A proven cross-process reap must survive restart as a terminal cancellation."""
    from repoforge.application.operations.recovery import recover_operation_work
    from repoforge.application.operations.work_admission import DurableWorkAdmission
    from repoforge.domain.operation_task import OperationState
    from repoforge.domain.operation_work import mark_work_child_started
    from repoforge.domain.operation_worker import OperationWorkerBinding

    env = create_forge_environment(tmp_path)
    config = load_config(env.config_path)
    application = build_application(config)
    service = CodingService(config, application=application)
    queue = application.context.operation_work_queue
    bindings = application.context.worker_bindings
    reaper = application.context.reaper
    assert queue is not None
    assert bindings is not None
    assert reaper is not None
    generation = application.context.config_generation or 12
    operation = DurableWorkAdmission(application.operations, queue).admit(
        OperationWorkRequest.profile(
            workspace_id="workspace-1",
            profile_name="quick",
            expected_head_sha="d" * 40,
            expected_fingerprint="e" * 64,
            config_generation=generation,
        ),
        operation_kind="workspace_run_profile",
    )
    now = datetime.fromisoformat(application.context.clock.now_iso())
    lease = (now + timedelta(seconds=90)).isoformat()
    claimed = queue.claim_next(
        owner_id="dead-worker",
        now=now.isoformat(),
        lease_expires_at=lease,
        compatible_kinds=frozenset({"profile"}),
        config_generation=generation,
    )
    assert claimed is not None
    application.operations.start(
        operation.operation_id,
        owner_id="dead-worker",
        lease_expires_at=lease,
        attempt=claimed.attempt,
        now=now.isoformat(),
    )
    started = mark_work_child_started(
        claimed,
        owner_id="dead-worker",
        attempt=claimed.attempt,
        now=(now + timedelta(milliseconds=1)).isoformat(),
    )
    queue.save(started, expected_updated_at=claimed.updated_at)
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        start_new_session=True,
    )
    try:
        child_token = reaper.read_start_token(child.pid)
        assert child_token is not None
        bindings.put(
            OperationWorkerBinding(
                operation_id=operation.operation_id,
                child_pid=child.pid,
                child_pgid=child.pid,
                child_start_token=child_token,
                server_pid=999_999_999,
                server_start_token="dead-worker-token",
                created_at=application.context.clock.now_iso(),
                owner_generation=generation,
                owner_id="dead-worker",
                attempt=claimed.attempt,
            )
        )

        service.operation_cancel(operation.operation_id)
        child.wait(timeout=3)
        recover_operation_work(
            application.operations,
            queue,
            now=application.context.clock.now_iso(),
            expected_config_generation=generation,
            worker_bindings=bindings,
            reaper=reaper,
        )

        assert (
            application.operations.status(operation.operation_id).state is OperationState.CANCELLED
        )
        assert queue.read(operation.operation_id) is None
        assert bindings.get(operation.operation_id) is None
    finally:
        if child.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(child.pid, signal.SIGKILL)
            child.wait(timeout=3)


def test_reaper_preserves_leaderless_group_without_identity_proof(tmp_path) -> None:
    """A reused leaderless PGID must not be signalled without durable member identity."""
    from repoforge.adapters.subprocess.os_process_reaper import OsProcessReaper
    from repoforge.domain.operation_worker import OperationWorkerBinding

    child_pid_path = tmp_path / "descendant.pid"
    leader_code = (
        "import subprocess, sys, time\n"
        "from pathlib import Path\n"
        "child = subprocess.Popen([sys.executable, '-c', "
        "'import time; time.sleep(30)'])\n"
        "Path(sys.argv[1]).write_text(str(child.pid), encoding='utf-8')\n"
        "time.sleep(0.2)\n"
    )
    leader = subprocess.Popen(
        [sys.executable, "-c", leader_code, str(child_pid_path)],
        start_new_session=True,
    )
    reaper = OsProcessReaper(term_grace_seconds=0.2)
    leader_token = reaper.read_start_token(leader.pid)
    assert leader_token is not None
    deadline = time.monotonic() + 3
    while not child_pid_path.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert child_pid_path.exists()
    child_pid = int(child_pid_path.read_text(encoding="utf-8"))
    leader.wait(timeout=3)
    server_token = reaper.read_start_token(os.getpid())
    assert server_token is not None
    binding = OperationWorkerBinding(
        operation_id="op-" + "f" * 24,
        child_pid=leader.pid,
        child_pgid=leader.pid,
        child_start_token=leader_token,
        server_pid=os.getpid(),
        server_start_token=server_token,
        created_at="2026-07-27T00:00:00+00:00",
    )
    try:
        outcome = reaper.reap(binding)

        assert outcome.attempted is False
        assert outcome.reaped is False
        assert outcome.still_alive is True
        assert "identity" in outcome.detail
        assert reaper.read_start_token(child_pid) is not None
    finally:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(leader.pid, signal.SIGKILL)


def test_one_unexecutable_item_does_not_stop_the_worker(tmp_path) -> None:
    """#307: an unreadable operation record must cost one item, not durable execution.

    The claim path reads the operation record. When that read raised, it propagated out of
    `run_once` and killed the worker THREAD -- so the queue kept filling and nothing
    drained it, with a pytest thread-exception warning as the only evidence (in production,
    silence). The bad item is discarded and the next one still runs.
    """
    from repoforge.application.operations.work_admission import DurableWorkAdmission
    from repoforge.application.operations.work_loop import OperationWorkLoop
    from repoforge.domain.operation_task import OperationState

    class SuccessfulHandlers:
        def execute(self, item, *, cancellation_token, progress):
            return {"workspace_id": item.request.workspace_id, "ok": True}

    env = create_forge_environment(tmp_path)
    application = build_application(load_config(env.config_path))
    queue = application.context.operation_work_queue
    assert queue is not None
    admission = DurableWorkAdmission(application.operations, queue)

    def _admit(profile: str, head: str):
        return admission.admit(
            OperationWorkRequest.profile(
                workspace_id="workspace-1",
                profile_name=profile,
                expected_head_sha=head * 40,
                expected_fingerprint="b" * 64,
                config_generation=12,
            ),
            operation_kind="workspace_run_profile",
        )

    broken = _admit("quick", "a")
    healthy = _admit("full", "c")
    # Destroy only the first item's operation record, leaving its claimable sidecar.
    application.context.operation_store.delete(broken.operation_id)
    assert queue.read(broken.operation_id) is not None

    worker = OperationWorkLoop(
        application.context,
        application.operations,
        SuccessfulHandlers(),
        owner_id="worker-test",
    )

    # The unexecutable item is consumed and reported, not raised.
    assert worker.run_once() is True
    assert queue.read(broken.operation_id) is None

    # The loop is still serving: the next item runs to a terminal success.
    assert worker.run_once() is True
    assert application.operations.status(healthy.operation_id).state is OperationState.SUCCEEDED
    assert worker.run_once() is False


def test_cancel_survives_a_heartbeat_landing_between_its_read_and_its_write(tmp_path) -> None:
    """A heartbeat must not defeat a cancel that asked for no optimistic concurrency.

    `request_cancel` reads the operation, decides, then saves with a compare-and-set
    against what it read. A running worker's heartbeat moves `updated_at` without changing
    anything the decision consumes, so it could lose that CAS and fail OPERATION_STALE --
    observed in CI on timestamps 60ms apart. The caller passed no `expected_updated_at`,
    so it asserted nothing and should not be told its assertion failed.
    """
    from dataclasses import replace as dataclass_replace

    from repoforge.domain.operation_task import next_operation_timestamp

    env = create_forge_environment(tmp_path)
    application = build_application(load_config(env.config_path), config_generation=1)
    operation = application.operations.create(
        kind="workspace_run_profile", phase="queued", cancel_supported=True
    )
    application.operations.start(operation.operation_id)

    inner = application.operations.store
    beats: list[str] = []

    class _HeartbeatOnFirstSave:
        """Emit exactly one heartbeat immediately before the first save reaches the store."""

        def __getattr__(self, name: str):
            return getattr(inner, name)

        def save(self, task, *, expected_updated_at=None):
            if not beats:
                current = inner.read(task.operation_id)
                bumped = dataclass_replace(
                    current,
                    updated_at=next_operation_timestamp(
                        current.updated_at, application.context.clock.now_iso()
                    ),
                )
                inner.save(bumped, expected_updated_at=current.updated_at)
                beats.append(bumped.updated_at)
            return inner.save(task, expected_updated_at=expected_updated_at)

    application.operations.store = _HeartbeatOnFirstSave()  # type: ignore[assignment]

    decision = application.operations.request_cancel(operation.operation_id)

    assert beats, "the test did not actually inject a heartbeat"
    assert decision.cancellation_requested is True
    assert application.operations.status(operation.operation_id).cancellation_requested_at


def test_cancel_still_reports_stale_when_the_caller_pinned_a_version(tmp_path) -> None:
    """The retry must not swallow a real conflict.

    A caller that supplies `expected_updated_at` is asserting something about the record.
    If the record moved, that assertion failed and the caller has to see it -- retrying
    silently against a newer version would answer a question it did not ask.
    """
    env = create_forge_environment(tmp_path)
    application = build_application(load_config(env.config_path), config_generation=1)
    operation = application.operations.create(
        kind="workspace_run_profile", phase="queued", cancel_supported=True
    )
    started = application.operations.start(operation.operation_id)

    with pytest.raises(RepoForgeError) as caught:
        application.operations.request_cancel(
            operation.operation_id,
            expected_updated_at="2000-01-01T00:00:00+00:00",
        )

    assert caught.value.code is ErrorCode.OPERATION_STALE
    assert started.updated_at in str(caught.value)
    assert application.operations.status(operation.operation_id).cancellation_requested_at is None
