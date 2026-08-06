from __future__ import annotations

import threading
import time
from pathlib import Path

from repoforge.adapters.persistence.json_execution_worker_binding_store import (
    JsonExecutionWorkerBindingStore,
)
from repoforge.application.operations.work_loop import OperationWorkLoop
from repoforge.application.runtime.supervisor import RuntimeSupervisor
from repoforge.domain.execution_worker import (
    ExecutionWorkerBinding,
    execution_worker_binding_from_payload,
    execution_worker_binding_payload,
)
from repoforge.domain.runtime import ChildProcess
from repoforge.ports.execution_worker import ExecutionWorkerProgressHealth
from repoforge.testing import FixedClock, InMemoryLockManager, SequenceIdGenerator

_WORKER_ID = "worker-0123456789ab"


def _binding(**overrides: object) -> ExecutionWorkerBinding:
    values: dict[str, object] = {
        "worker_id": _WORKER_ID,
        "pid": 4242,
        "pgid": 4242,
        "process_start_token": "start-token",
        "generation": 12,
        "release_sha": "0123abc",
        "supervisor_pid": 4141,
        "supervisor_process_identity": "a" * 64,
        "correlation_id": "correlation",
        "started_at": "2026-08-06T00:00:00+00:00",
        "state": "running",
        "heartbeat_at": None,
        "loop_state": None,
        "current_operation_id": None,
        "last_recovery_at": None,
    }
    values.update(overrides)
    return ExecutionWorkerBinding(**values)


def test_old_execution_worker_binding_payload_decodes_without_progress_fields() -> None:
    payload = execution_worker_binding_payload(_binding())
    for field in ("heartbeat_at", "loop_state", "current_operation_id", "last_recovery_at"):
        payload.pop(field)

    decoded = execution_worker_binding_from_payload(payload)

    assert decoded.heartbeat_at is None
    assert decoded.loop_state is None
    assert decoded.current_operation_id is None
    assert decoded.last_recovery_at is None


def test_store_updates_progress_heartbeat_without_changing_worker_identity(tmp_path: Path) -> None:
    store = JsonExecutionWorkerBindingStore(tmp_path / "state", InMemoryLockManager())
    original = _binding()
    store.put(original)

    updated = store.update_heartbeat(
        _WORKER_ID,
        heartbeat_at="2026-08-06T00:00:10+00:00",
        loop_state="executing",
        current_operation_id="op-0123456789abcdef01234567",
        recovery_completed=False,
    )

    assert updated is not None
    assert updated.pid == original.pid
    assert updated.process_start_token == original.process_start_token
    assert updated.heartbeat_at == "2026-08-06T00:00:10+00:00"
    assert updated.loop_state == "executing"
    assert updated.current_operation_id == "op-0123456789abcdef01234567"
    assert updated.last_recovery_at is None

    recovered = store.update_heartbeat(
        _WORKER_ID,
        heartbeat_at="2026-08-06T00:00:20+00:00",
        loop_state="idle",
        current_operation_id=None,
        recovery_completed=True,
    )
    assert recovered is not None
    assert recovered.last_recovery_at == "2026-08-06T00:00:20+00:00"


def test_work_loop_emits_recovery_idle_and_stopping_progress(forge_env) -> None:
    states: list[tuple[str, str | None, bool]] = []

    class NoWorkHandlers:
        def execute(self, item, *, cancellation_token, progress):
            raise AssertionError("no work should be admitted")

    loop = OperationWorkLoop(
        forge_env.service.application.context,
        forge_env.service.operations,
        NoWorkHandlers(),
        idle_poll_seconds=0.01,
        recovery_interval_seconds=0.01,
        worker_heartbeat=lambda state, operation_id, recovery: states.append(
            (state, operation_id, recovery)
        ),
    )
    stop = threading.Event()
    thread = threading.Thread(target=loop.run_until_stopped, args=(stop,))
    thread.start()
    deadline = time.monotonic() + 2
    while not any(state == "idle" for state, _, _ in states) and time.monotonic() < deadline:
        time.sleep(0.01)
    stop.set()
    loop.request_stop()
    thread.join(timeout=2)

    assert thread.is_alive() is False
    assert any(state == "recovering" and recovery for state, _, recovery in states)
    assert any(state == "idle" for state, _, _ in states)
    assert states[-1][0] == "stopping"


def test_supervisor_replaces_a_live_worker_only_after_progress_is_stale(tmp_path: Path) -> None:
    class Worker:
        def __init__(self) -> None:
            self.started: list[int] = []
            self.alive: set[int] = set()
            self.stale: set[int] = set()
            self.events: list[tuple[str, int]] = []

        def start(self, generation, *, env, log_path, correlation_id):
            del env, log_path, correlation_id
            child = ChildProcess(500 + len(self.started), f"{generation:064x}", "now")
            self.started.append(generation)
            self.alive.add(child.pid)
            self.events.append(("start", child.pid))
            return child

        def is_alive(self, child):
            return child.pid in self.alive

        def progress_health(self, child, *, now, stale_after_seconds):
            del now, stale_after_seconds
            healthy = child.pid not in self.stale
            return ExecutionWorkerProgressHealth(
                process_alive=True,
                heartbeat_available=True,
                heartbeat_age_seconds=120.0 if not healthy else 1.0,
                progress_healthy=healthy,
                loop_state="executing",
                current_operation_id=None,
                detail="stale heartbeat" if not healthy else "fresh heartbeat",
            )

        def terminate(self, child, *, grace_seconds):
            del grace_seconds
            self.events.append(("terminate", child.pid))
            self.alive.discard(child.pid)

    worker = Worker()
    supervisor = RuntimeSupervisor(
        store=object(),
        configs=object(),
        locks=object(),
        control=object(),
        mcp_control=object(),
        tunnel=object(),
        profile_store=object(),
        clock=FixedClock("2026-08-06T00:02:00+00:00"),
        ids=SequenceIdGenerator(("worker",)),
        processes=object(),
        mcp_runtime_path=tmp_path / "mcp.json",
        log_path=tmp_path / "runtime.log",
        execution_worker=worker,
        execution_worker_log_path=tmp_path / "worker.log",
        execution_worker_stale_seconds=30.0,
    )

    supervisor._ensure_execution_worker(12, environment={}, correlation_id="first")
    first = supervisor._execution_child
    assert first is not None
    supervisor._ensure_execution_worker(12, environment={}, correlation_id="fresh")
    worker.stale.add(first.pid)
    supervisor._ensure_execution_worker(12, environment={}, correlation_id="stale")

    assert worker.started == [12, 12]
    assert worker.events == [
        ("start", first.pid),
        ("terminate", first.pid),
        ("start", first.pid + 1),
    ]
