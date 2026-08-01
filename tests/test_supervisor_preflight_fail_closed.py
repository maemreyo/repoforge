"""Resident fail-closed supervisor preflight (#367).

A supervisor running a release whose packaged contract identity diverges from its
in-process registry must never spawn an execution worker or tunnel child: that child
would die on every spawn and launchd would crash-loop the supervisor. The supervisor
runs a deterministic preflight before any child, and on failure writes a typed
FAIL_CLOSED record, keeps the control plane answerable, and stays resident -- it never
exits into launchd's restart.
"""

from __future__ import annotations

import threading
import time
from contextlib import nullcontext
from pathlib import Path

from repoforge.application.runtime.supervisor import RuntimeSupervisor
from repoforge.domain.errors import ConfigError
from repoforge.domain.runtime import (
    ControlCommand,
    ControlRequest,
    RuntimePhase,
    RuntimeRecord,
    TunnelProfile,
)
from repoforge.testing import FixedClock, SequenceIdGenerator

_PROFILE = TunnelProfile("a" * 64, "repoforge", "tunnel-client", "1.0", ("rf", "serve"))
_SHA = "0123abc"


def _record(
    phase: RuntimePhase,
    *,
    running_release_sha: str | None = None,
    fail_closed_since: str | None = None,
    last_error_code: str | None = None,
) -> RuntimeRecord:
    return RuntimeRecord(
        protocol_version=1,
        phase=phase,
        pid=42,
        process_identity="a" * 64,
        active_generation=1 if phase is RuntimePhase.HEALTHY else None,
        accepted_generation=1,
        tunnel_profile="repoforge",
        tunnel_profile_fingerprint="f",
        tool_surface_hash="t",
        started_at="2026-07-13T00:00:00+00:00",
        updated_at="2026-07-13T00:00:01+00:00",
        correlation_id="c",
        running_release_sha=running_release_sha,
        last_error_code=last_error_code,
        fail_closed_since=fail_closed_since,
    )


class _Locks:
    def lock(self, name: str, *, timeout_seconds=None, metadata=None):
        del name, timeout_seconds, metadata
        return nullcontext()


class _Server:
    def __init__(self) -> None:
        self.handler = None

    def start(self, handler) -> None:
        self.handler = handler

    def close(self) -> None:
        pass

    def is_serving(self) -> bool:
        return True

    def serving_diagnostic(self) -> str:
        return "fake control server"


class _Store:
    def __init__(self, prior: RuntimeRecord | None = None) -> None:
        self.record = prior
        self.writes: list[RuntimeRecord] = []

    def read(self) -> RuntimeRecord | None:
        return self.record

    def write(self, record: RuntimeRecord) -> None:
        self.record = record
        self.writes.append(record)


class _Configs:
    def __init__(self) -> None:
        self.cleared: list[int] = []

    def clear_activation_target(self, *, expected_generation: int) -> None:
        self.cleared.append(expected_generation)


class _Mcp:
    def request(self, request, *, timeout_seconds=10.0):
        del request, timeout_seconds
        raise AssertionError("MCP control must not be reached while fail-closed")


class _Processes:
    def identity(self, pid: int) -> str | None:
        return "a" * 64


class _Tunnel:
    def __init__(self) -> None:
        self.starts = 0

    def initialize(self, profile, *, env) -> None:
        del profile, env

    def doctor(self, profile, *, env):
        del profile, env
        return True, "fake doctor"

    def start(self, profile, *, env, log_path, correlation_id):
        del profile, env, log_path, correlation_id
        self.starts += 1


class _ProfileStore:
    def fingerprint(self) -> str:
        return ""

    def commit(self, profile) -> None:
        del profile


class _ExecutionWorker:
    def __init__(self) -> None:
        self.started: list[int] = []

    def start(self, generation, *, env, log_path, correlation_id):
        del env, log_path, correlation_id
        self.started.append(generation)

    def is_alive(self, child) -> bool:
        del child
        return False

    def terminate(self, child, *, grace_seconds) -> None:
        del child, grace_seconds


def _supervisor(
    *,
    store: _Store,
    preflight,
    tmp_path: Path,
    tunnel: _Tunnel,
    worker: _ExecutionWorker,
) -> tuple[RuntimeSupervisor, _Server]:
    server = _Server()
    supervisor = RuntimeSupervisor(
        store=store,
        configs=_Configs(),
        locks=_Locks(),
        control=server,
        mcp_control=_Mcp(),
        tunnel=tunnel,
        profile_store=_ProfileStore(),
        clock=FixedClock("2026-07-13T00:00:00+00:00"),
        ids=SequenceIdGenerator(("supervisor", "health")),
        processes=_Processes(),
        mcp_runtime_path=tmp_path / "runtime.json",
        log_path=tmp_path / "runtime.log",
        execution_worker=worker,
        execution_worker_log_path=tmp_path / "execution-worker.log",
        preflight=preflight,
    )
    return supervisor, server


def _run_and_wait_fail_closed(
    supervisor: RuntimeSupervisor, store: _Store
) -> tuple[list[int], threading.Thread]:
    result: list[int] = []
    thread = threading.Thread(
        target=lambda: result.append(
            supervisor.run(
                generation=1,
                profile=_PROFILE,
                tool_surface_hash="b" * 64,
                environment={},
            )
        )
    )
    thread.start()
    deadline = time.monotonic() + 5
    while (
        store.record is None or store.record.phase is not RuntimePhase.FAIL_CLOSED
    ) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert store.record is not None
    assert store.record.phase is RuntimePhase.FAIL_CLOSED
    return result, thread


def _shutdown_and_join(server: _Server, thread: threading.Thread, result: list[int]) -> None:
    server.handler(ControlRequest(1, ControlCommand.SHUTDOWN, "corr"))
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert result == [0]


def test_preflight_failure_enters_resident_fail_closed_without_spawning_children(
    tmp_path: Path,
) -> None:
    store = _Store()
    tunnel = _Tunnel()
    worker = _ExecutionWorker()

    def preflight() -> None:
        raise ConfigError(
            "CONTRACT_ARTIFACT_MISMATCH: packaged contract identity differs from the "
            "in-process registry"
        )

    supervisor, server = _supervisor(
        store=store, preflight=preflight, tmp_path=tmp_path, tunnel=tunnel, worker=worker
    )
    result, thread = _run_and_wait_fail_closed(supervisor, store)

    record = store.record
    assert record.last_error_code == "CONTRACT_ARTIFACT_MISMATCH"
    assert record.fail_closed_since is not None
    assert tunnel.starts == 0
    assert worker.started == []
    # The control plane stays answerable in FAIL_CLOSED.
    response = server.handler(ControlRequest(1, ControlCommand.STATUS, "corr"))
    assert response.ok
    assert dict(response.payload)["record"] == "fail_closed"

    _shutdown_and_join(server, thread, result)
    assert store.record.phase is RuntimePhase.STOPPED


def test_a_fresh_supervisor_honors_a_prior_fail_closed_state_for_the_same_release(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("REPOFORGE_RUNNING_RELEASE_SHA", _SHA)
    prior = _record(
        RuntimePhase.FAIL_CLOSED,
        running_release_sha=_SHA,
        fail_closed_since="2026-07-12T00:00:00+00:00",
        last_error_code="CONTRACT_ARTIFACT_MISMATCH",
    )
    store = _Store(prior)
    tunnel = _Tunnel()
    worker = _ExecutionWorker()
    preflight_calls: list[int] = []

    def preflight() -> None:
        preflight_calls.append(1)

    supervisor, server = _supervisor(
        store=store, preflight=preflight, tmp_path=tmp_path, tunnel=tunnel, worker=worker
    )
    result, thread = _run_and_wait_fail_closed(supervisor, store)

    record = store.record
    assert record.last_error_code == "CONTRACT_ARTIFACT_MISMATCH"
    assert record.fail_closed_since == "2026-07-12T00:00:00+00:00"
    assert preflight_calls == []
    assert tunnel.starts == 0
    assert worker.started == []

    _shutdown_and_join(server, thread, result)


def test_a_different_release_is_not_bound_by_a_prior_fail_closed_state(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("REPOFORGE_RUNNING_RELEASE_SHA", "9999fff")
    prior = _record(
        RuntimePhase.FAIL_CLOSED,
        running_release_sha=_SHA,
        fail_closed_since="2026-07-12T00:00:00+00:00",
        last_error_code="CONTRACT_ARTIFACT_MISMATCH",
    )
    store = _Store(prior)
    tunnel = _Tunnel()
    worker = _ExecutionWorker()
    preflight_calls: list[int] = []

    def preflight() -> None:
        preflight_calls.append(1)
        raise ConfigError("NEW_PREFLIGHT_FAILURE: this release is bad too")

    supervisor, server = _supervisor(
        store=store, preflight=preflight, tmp_path=tmp_path, tunnel=tunnel, worker=worker
    )
    result, thread = _run_and_wait_fail_closed(supervisor, store)

    record = store.record
    assert preflight_calls == [1]
    assert record.last_error_code == "NEW_PREFLIGHT_FAILURE"

    _shutdown_and_join(server, thread, result)


def test_an_identityless_fail_closed_record_does_not_latch_a_new_supervisor(
    tmp_path: Path, monkeypatch
) -> None:
    """Without a proven release identity, the durable latch is NOT inherited (#420).

    A legacy/manual start may lack REPOFORGE_RUNNING_RELEASE_SHA; inheriting the
    prior supervisor's fail-closed state would wrongly hold a healthy release
    resident. The deterministic preflight re-runs instead (safe: no child spawned).
    """
    monkeypatch.setenv("REPOFORGE_RUNNING_RELEASE_SHA", _SHA)
    prior = _record(
        RuntimePhase.FAIL_CLOSED,
        running_release_sha=None,
        fail_closed_since="2026-07-12T00:00:00+00:00",
        last_error_code="CONTRACT_ARTIFACT_MISMATCH",
    )
    store = _Store(prior)
    tunnel = _Tunnel()
    worker = _ExecutionWorker()
    preflight_calls: list[int] = []

    def preflight() -> None:
        preflight_calls.append(1)

    supervisor, server = _supervisor(
        store=store, preflight=preflight, tmp_path=tmp_path, tunnel=tunnel, worker=worker
    )
    result, thread = _run_and_wait_fail_closed(supervisor, store)
    _shutdown_and_join(server, thread, result)

    assert preflight_calls == [1]
    assert store.record.last_error_code == "CONTRACT_ARTIFACT_MISMATCH"


def test_a_missing_current_release_sha_does_not_latch_either(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("REPOFORGE_RUNNING_RELEASE_SHA", raising=False)
    prior = _record(
        RuntimePhase.FAIL_CLOSED,
        running_release_sha=_SHA,
        fail_closed_since="2026-07-12T00:00:00+00:00",
        last_error_code="CONTRACT_ARTIFACT_MISMATCH",
    )
    store = _Store(prior)
    tunnel = _Tunnel()
    worker = _ExecutionWorker()
    preflight_calls: list[int] = []

    def preflight() -> None:
        preflight_calls.append(1)

    supervisor, server = _supervisor(
        store=store, preflight=preflight, tmp_path=tmp_path, tunnel=tunnel, worker=worker
    )
    result, thread = _run_and_wait_fail_closed(supervisor, store)
    _shutdown_and_join(server, thread, result)

    assert preflight_calls == [1]
