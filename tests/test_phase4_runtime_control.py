from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

from repoforge.adapters.runtime import (
    InProcessOperationGate,
    UnixRuntimeControlClient,
    UnixRuntimeControlServer,
)
from repoforge.application.runtime.activation import GenerationActivator
from repoforge.domain.config_generation import CapabilityDeltaKind, ConfigGeneration
from repoforge.domain.errors import ConfigError
from repoforge.domain.runtime import (
    ControlCommand,
    ControlRequest,
    ControlResponse,
    RuntimePhase,
    RuntimeRecord,
    transition,
)
from repoforge.testing import FixedClock, SequenceIdGenerator


class _NullRestartHistory:
    """In-memory fake: durable restart-history ledger (#448 Slice 4).

    Most tests here don't exercise restart-history semantics directly, so this
    just reflects whatever was last written -- `None` until then, like a fresh
    install with no ledger yet.
    """

    def __init__(self) -> None:
        self.record: object | None = None

    def read(self) -> object | None:
        return self.record

    def write(self, record: object) -> None:
        self.record = record

    def record_restart(
        self, *, incarnation_id: str, reason: str | None, occurred_at: str, event_id: str
    ) -> object:
        from repoforge.domain.runtime import RestartHistoryRecord

        current = self.record
        if current is not None and getattr(current, "last_event_id", None) == event_id:
            return current
        self.record = RestartHistoryRecord(
            protocol_version=1,
            restarts_total=(getattr(current, "restarts_total", 0) if current is not None else 0)
            + 1,
            last_restart_at=occurred_at,
            incarnation_id=incarnation_id,
            updated_at=occurred_at,
            last_event_id=event_id,
            last_restart_reason=reason,
            provenance="durable",
        )
        return self.record

    def seed_if_missing(
        self,
        *,
        restarts_total: int,
        last_restart_at: str | None,
        incarnation_id: str,
        occurred_at: str,
    ) -> object:
        from repoforge.domain.runtime import RestartHistoryRecord

        if self.record is not None:
            return self.record
        self.record = RestartHistoryRecord(
            protocol_version=1,
            restarts_total=max(0, restarts_total),
            last_restart_at=last_restart_at,
            incarnation_id=incarnation_id,
            updated_at=occurred_at,
            provenance="legacy_runtime_record",
        )
        return self.record


def _generation(number: int, delta: CapabilityDeltaKind) -> ConfigGeneration:
    return ConfigGeneration(
        number,
        "a" * 64,
        "b" * 64,
        (),
        "now",
        "test",
        None,
        None,
        delta,
        number - 1 or None,
        active=False,
    )


def _record(phase: RuntimePhase, generation: int | None = 1) -> RuntimeRecord:
    healthy = phase is RuntimePhase.HEALTHY
    return RuntimeRecord(
        1,
        phase,
        100 if healthy else None,
        "a" * 64 if healthy else None,
        generation,
        generation or 1,
        "p",
        "f",
        "t",
        "now" if healthy else None,
        "now",
        "c",
        child_pid=101 if healthy else None,
        child_process_identity="b" * 64 if healthy else None,
    )


def test_runtime_state_machine_rejects_invalid_transition() -> None:
    healthy = _record(RuntimePhase.HEALTHY)
    draining = transition(healthy, RuntimePhase.DRAINING, updated_at="later", correlation_id="x")
    assert draining.phase is RuntimePhase.DRAINING
    with pytest.raises(ValueError, match="Invalid runtime transition"):
        transition(draining, RuntimePhase.STOPPED, updated_at="later", correlation_id="x")


def test_unix_control_protocol_is_owner_only_versioned_and_allowlisted(tmp_path: Path) -> None:
    path = tmp_path / "control.sock"
    server = UnixRuntimeControlServer(path)
    seen: list[ControlCommand] = []

    def handler(request: ControlRequest) -> ControlResponse:
        seen.append(request.command)
        return ControlResponse(1, True, request.correlation_id, "ok", (("uid", os.getuid()),))

    server.start(handler)
    try:
        response = UnixRuntimeControlClient(path).request(
            ControlRequest(1, ControlCommand.PING, "abc")
        )
        assert response.ok and dict(response.payload)["uid"] == os.getuid()
        assert seen == [ControlCommand.PING]
        invalid = UnixRuntimeControlClient(path).request(
            ControlRequest(2, ControlCommand.PING, "bad")
        )
        assert not invalid.ok
        assert invalid.error_code == "ConfigError"
        assert "Unsupported runtime control protocol" in (invalid.message or "")
    finally:
        server.close()
    assert not path.exists()


def test_unix_control_hashes_long_logical_socket_paths(tmp_path: Path) -> None:
    logical = tmp_path / ("nested-" + "x" * 160) / "control.sock"
    server = UnixRuntimeControlServer(logical)

    server.start(lambda request: ControlResponse(1, True, request.correlation_id, "ok"))
    try:
        response = UnixRuntimeControlClient(logical).request(
            ControlRequest(1, ControlCommand.PING, "long-path")
        )
        assert response.ok
        assert server.bound_path != logical
        assert len(os.fsencode(server.bound_path)) <= 100
        assert not logical.exists()
    finally:
        bound = server.bound_path
        server.close()
    assert not bound.exists()


def test_gate_waits_for_inflight_read_and_rejects_new_write() -> None:
    gate = InProcessOperationGate()
    entered = threading.Event()
    release = threading.Event()

    def reader() -> None:
        with gate.operation("read", mutating=False):
            entered.set()
            release.wait(2)

    thread = threading.Thread(target=reader)
    thread.start()
    assert entered.wait(1)
    gate.begin_drain(reason="reload", correlation_id="c")
    assert not gate.wait_for_idle(0.05)
    with (
        pytest.raises(ConfigError, match="RUNTIME_RELOADING"),
        gate.operation("write", mutating=True),
    ):
        pass
    release.set()
    thread.join(2)
    assert gate.wait_for_idle(0.2)


class MemoryTunnelProfileStore:
    def __init__(self, fingerprint: str | None = None) -> None:
        self.value = fingerprint
        self.commits: list[str] = []

    def fingerprint(self) -> str | None:
        return self.value

    def commit(self, profile) -> None:
        self.value = profile.fingerprint
        self.commits.append(profile.fingerprint)


class FakeConfigStore:
    def __init__(self, active: ConfigGeneration):
        self.current_item = active
        self.active_item = replace(active, active=True)
        self.target_item: ConfigGeneration | None = None
        self.staged: list[int] = []
        self.activations: list[int] = []

    @property
    def source_path(self) -> Path:
        return Path("/config")

    def current(self) -> ConfigGeneration | None:
        return self.current_item

    def active(self) -> ConfigGeneration | None:
        return self.active_item

    def activation_target(self) -> ConfigGeneration | None:
        return self.target_item

    def stage_activation(
        self, generation: int, *, expected_active: int | None = None
    ) -> ConfigGeneration:
        if expected_active is not None and self.active_item.generation != expected_active:
            raise ConfigError("stale")
        self.staged.append(generation)
        self.target_item = _generation(generation, CapabilityDeltaKind.EQUIVALENT)
        return self.target_item

    def clear_activation_target(self, *, expected_generation: int | None = None) -> None:
        if (
            expected_generation is not None
            and self.target_item is not None
            and self.target_item.generation != expected_generation
        ):
            raise ConfigError("stale target")
        self.target_item = None

    def history(self):
        return (self.current_item,)

    def read_source_text(self):
        return ""

    def read_resolved_text(self, generation=None):
        return ""

    def accept(self, mutation):
        raise AssertionError

    def activate(self, generation: int, *, expected_active: int | None = None) -> ConfigGeneration:
        if expected_active is not None and self.active_item.generation != expected_active:
            raise ConfigError("stale")
        if self.target_item is None or self.target_item.generation != generation:
            raise ConfigError("target mismatch")
        self.activations.append(generation)
        self.active_item = replace(
            _generation(generation, CapabilityDeltaKind.EQUIVALENT), active=True
        )
        self.target_item = None
        return self.active_item

    def rollback(self, generation, *, expected_active, approval_token=None):
        self.stage_activation(generation, expected_active=expected_active)
        return self.activate(generation, expected_active=expected_active)


class FakeRuntimeStore:
    def __init__(self, record: RuntimeRecord | None = None):
        self.record = record

    def read(self) -> RuntimeRecord | None:
        return self.record

    def write(self, record: RuntimeRecord) -> None:
        self.record = record

    def clear(self, *, expected_pid: int | None = None) -> None:
        self.record = None

    def peek_restart_evidence(self) -> tuple[int, str | None] | None:
        return None


class FakeControl:
    def __init__(self, runtime: FakeRuntimeStore):
        self.runtime = runtime
        self.commands: list[ControlCommand] = []

    def request(self, request: ControlRequest, *, timeout_seconds: float = 10.0) -> ControlResponse:
        self.commands.append(request.command)
        if request.command is ControlCommand.SHUTDOWN:
            self.runtime.record = _record(RuntimePhase.STOPPED, None)
        return ControlResponse(1, True, request.correlation_id, "ok")


class FakeLauncher:
    def __init__(self, runtime: FakeRuntimeStore, failures: int = 0):
        self.runtime = runtime
        self.failures = failures
        self.started: list[int] = []
        self.configs: FakeConfigStore | None = None

    def start(self, config_path: Path, *, foreground: bool, extra_env: dict[str, str]) -> int:
        del config_path, foreground, extra_env
        if self.failures:
            self.failures -= 1
            raise OSError("injected launch failure")
        assert self.configs is not None
        assert self.configs.target_item is not None
        generation = self.configs.target_item.generation
        self.configs.activate(generation, expected_active=self.configs.active_item.generation)
        self.started.append(generation)
        self.runtime.record = _record(RuntimePhase.HEALTHY, generation)
        return 100 + generation

    def force_stop(self, record: RuntimeRecord, *, grace_seconds: float = 5.0) -> bool:
        del record, grace_seconds
        self.runtime.record = _record(RuntimePhase.STOPPED, None)
        return True


def _activator(
    configs: FakeConfigStore, runtime: FakeRuntimeStore, launcher: FakeLauncher
) -> GenerationActivator:
    launcher.configs = configs
    control = FakeControl(runtime)
    return GenerationActivator(
        configs=configs,
        runtime=runtime,
        mcp_control=control,
        supervisor_control=control,
        launcher=launcher,
        ids=SequenceIdGenerator(("correlation",)),
        clock=FixedClock("2026-07-13T00:00:00+00:00"),
        config_path=Path("/config"),
        health_timeout_seconds=0.1,
        drain_timeout_seconds=0.1,
    )


def test_failed_expansion_rolls_back_to_last_known_good() -> None:
    previous = _generation(1, CapabilityDeltaKind.EXPANSION)
    configs = FakeConfigStore(previous)
    runtime = FakeRuntimeStore(_record(RuntimePhase.HEALTHY, 1))
    launcher = FakeLauncher(runtime, failures=1)
    result = _activator(configs, runtime, launcher).activate(
        _generation(2, CapabilityDeltaKind.EXPANSION), extra_env={}
    )
    assert result.status == "rolled_back"
    assert configs.staged == [2, 1]
    assert configs.activations == [1]
    assert launcher.started == [1]


def test_failed_restriction_never_restores_revoked_capability() -> None:
    previous = _generation(1, CapabilityDeltaKind.EXPANSION)
    configs = FakeConfigStore(previous)
    runtime = FakeRuntimeStore(_record(RuntimePhase.HEALTHY, 1))
    launcher = FakeLauncher(runtime, failures=1)
    with pytest.raises(ConfigError, match="RESTRICTIVE_ACTIVATION_FAILED"):
        _activator(configs, runtime, launcher).activate(
            _generation(2, CapabilityDeltaKind.RESTRICTION), extra_env={}
        )
    assert configs.staged == [2]
    assert configs.activations == []
    assert runtime.record is not None
    assert runtime.record.phase is RuntimePhase.FAIL_CLOSED


def test_expansion_drain_timeout_keeps_old_runtime_and_never_launches() -> None:
    previous = _generation(1, CapabilityDeltaKind.EXPANSION)
    configs = FakeConfigStore(previous)
    runtime = FakeRuntimeStore(_record(RuntimePhase.HEALTHY, 1))
    launcher = FakeLauncher(runtime)
    launcher.configs = configs

    class DrainTimeoutControl(FakeControl):
        def request(
            self, request: ControlRequest, *, timeout_seconds: float = 10.0
        ) -> ControlResponse:
            del timeout_seconds
            self.commands.append(request.command)
            return ControlResponse(
                1,
                request.command is ControlCommand.RESUME,
                request.correlation_id,
                "ok" if request.command is ControlCommand.RESUME else "drain_timeout",
            )

    mcp = DrainTimeoutControl(runtime)
    supervisor = FakeControl(runtime)
    activator = GenerationActivator(
        configs=configs,
        runtime=runtime,
        mcp_control=mcp,
        supervisor_control=supervisor,
        launcher=launcher,
        ids=SequenceIdGenerator(("correlation",)),
        clock=FixedClock("2026-07-13T00:00:00+00:00"),
        config_path=Path("/config"),
        health_timeout_seconds=0.1,
        drain_timeout_seconds=0.01,
    )
    with pytest.raises(ConfigError, match="RUNTIME_DRAIN_TIMEOUT"):
        activator.activate(_generation(2, CapabilityDeltaKind.EXPANSION), extra_env={})
    assert launcher.started == []
    assert ControlCommand.SHUTDOWN not in supervisor.commands
    assert mcp.commands == [
        ControlCommand.RELOAD,
        ControlCommand.DRAIN,
        ControlCommand.RESUME,
    ]
    assert runtime.record is not None and runtime.record.phase is RuntimePhase.HEALTHY
    assert runtime.record.active_generation == 1


def test_restriction_drain_timeout_enters_fail_closed_without_interrupting_inflight_work() -> None:
    previous = _generation(1, CapabilityDeltaKind.EXPANSION)
    configs = FakeConfigStore(previous)
    runtime = FakeRuntimeStore(_record(RuntimePhase.HEALTHY, 1))
    launcher = FakeLauncher(runtime)
    launcher.configs = configs

    class RestrictionControl(FakeControl):
        def request(
            self, request: ControlRequest, *, timeout_seconds: float = 10.0
        ) -> ControlResponse:
            del timeout_seconds
            self.commands.append(request.command)
            return ControlResponse(
                1,
                request.command is ControlCommand.FAIL_CLOSED,
                request.correlation_id,
                "fail_closed" if request.command is ControlCommand.FAIL_CLOSED else "drain_timeout",
            )

    mcp = RestrictionControl(runtime)
    supervisor = FakeControl(runtime)
    activator = GenerationActivator(
        configs=configs,
        runtime=runtime,
        mcp_control=mcp,
        supervisor_control=supervisor,
        launcher=launcher,
        ids=SequenceIdGenerator(("correlation",)),
        clock=FixedClock("2026-07-13T00:00:00+00:00"),
        config_path=Path("/config"),
        health_timeout_seconds=0.1,
        drain_timeout_seconds=0.01,
    )
    with pytest.raises(ConfigError, match="fail-closed"):
        activator.activate(_generation(2, CapabilityDeltaKind.RESTRICTION), extra_env={})
    assert launcher.started == []
    assert ControlCommand.SHUTDOWN not in supervisor.commands
    assert mcp.commands == [
        ControlCommand.RELOAD,
        ControlCommand.DRAIN,
        ControlCommand.FAIL_CLOSED,
    ]
    assert runtime.record is not None and runtime.record.phase is RuntimePhase.FAIL_CLOSED
    assert runtime.record.accepted_generation == 2


def test_restriction_forced_stop_failure_preserves_owned_process_identity() -> None:
    previous = _generation(1, CapabilityDeltaKind.EXPANSION)
    configs = FakeConfigStore(previous)
    runtime = FakeRuntimeStore(_record(RuntimePhase.HEALTHY, 1))

    class UnreachableControl(FakeControl):
        def request(
            self, request: ControlRequest, *, timeout_seconds: float = 10.0
        ) -> ControlResponse:
            del timeout_seconds
            self.commands.append(request.command)
            return ControlResponse(1, False, request.correlation_id, "unreachable")

    class UnstoppableLauncher(FakeLauncher):
        def force_stop(self, record: RuntimeRecord, *, grace_seconds: float = 5.0) -> bool:
            del record, grace_seconds
            return False

    class ImmediateActivator(GenerationActivator):
        def _wait_stopped(self, timeout: float = 20.0) -> bool:
            del timeout
            return False

    launcher = UnstoppableLauncher(runtime)
    launcher.configs = configs
    mcp = UnreachableControl(runtime)
    supervisor = UnreachableControl(runtime)
    activator = ImmediateActivator(
        configs=configs,
        runtime=runtime,
        mcp_control=mcp,
        supervisor_control=supervisor,
        launcher=launcher,
        ids=SequenceIdGenerator(("correlation",)),
        clock=FixedClock("2026-07-13T00:00:00+00:00"),
        config_path=Path("/config"),
        health_timeout_seconds=0.1,
        drain_timeout_seconds=0.01,
    )

    with pytest.raises(ConfigError, match="RESTRICTION_FORCED_STOP") as error:
        activator.activate(_generation(2, CapabilityDeltaKind.RESTRICTION), extra_env={})

    assert "could not be confirmed stopped" in str(error.value)
    assert runtime.record is not None
    assert runtime.record.phase is RuntimePhase.FAIL_CLOSED
    assert runtime.record.pid == 100
    assert runtime.record.process_identity == "a" * 64
    assert runtime.record.child_pid == 101
    assert runtime.record.child_process_identity == "b" * 64
    assert runtime.record.active_generation == 1
    assert runtime.record.accepted_generation == 2
    assert runtime.record.last_error is not None
    assert "could not be confirmed stopped" in runtime.record.last_error


def test_async_activation_requires_explicitly_disabling_rollback() -> None:
    previous = _generation(1, CapabilityDeltaKind.EXPANSION)
    configs = FakeConfigStore(previous)
    runtime = FakeRuntimeStore()
    launcher = FakeLauncher(runtime)
    with pytest.raises(ValueError, match="cannot guarantee automatic rollback"):
        _activator(configs, runtime, launcher).activate(
            _generation(2, CapabilityDeltaKind.EXPANSION),
            extra_env={},
            wait_for_health=False,
            rollback_on_failure=True,
        )


def test_supervisor_commits_active_generation_only_after_health(tmp_path: Path) -> None:
    from contextlib import nullcontext

    from repoforge.application.runtime.supervisor import RuntimeSupervisor
    from repoforge.domain.runtime import ChildProcess, TunnelProfile

    class Locks:
        def lock(self, name: str, *, timeout_seconds=None, metadata=None):
            del name, timeout_seconds, metadata
            return nullcontext()

    class Server:
        def start(self, handler):
            self.handler = handler

        def close(self):
            pass

        def is_serving(self) -> bool:
            return True

        def is_healthy(self) -> bool:
            return True

        def serving_diagnostic(self) -> str:
            return "fake control server"

    class Mcp:
        def __init__(self):
            self.on_health = lambda: None

        def request(self, request, *, timeout_seconds=10.0):
            del timeout_seconds
            self.on_health()
            return ControlResponse(1, True, request.correlation_id, "healthy")

    class Processes:
        def identity(self, pid: int) -> str | None:
            return "f" * 64 if pid > 0 else None

    class Tunnel:
        def __init__(self):
            self.alive = True
            self.terminated = False
            self.initialize_calls = 0

        def executable_version(self):
            return "1.0"

        def initialize(self, profile, *, env):
            del profile, env
            self.initialize_calls += 1

        def doctor(self, profile, *, env):
            del profile, env
            return (True, "ok")

        def start(self, profile, *, env, log_path, correlation_id):
            del profile, env, log_path, correlation_id
            return ChildProcess(222, "f" * 64, "now")

        def terminate(self, child, *, grace_seconds):
            del child, grace_seconds
            self.alive = False
            self.terminated = True

        def is_alive(self, child):
            del child
            return self.alive

    class ExecutionWorker:
        def __init__(self):
            self.started_generations = []
            self.terminated = False
            self.alive = True

        def start(self, generation, *, env, log_path, correlation_id):
            del env, log_path, correlation_id
            self.started_generations.append(generation)
            return ChildProcess(333, "e" * 64, "now")

        def terminate(self, child, *, grace_seconds):
            del child, grace_seconds
            self.alive = False
            self.terminated = True

        def is_alive(self, child):
            del child
            return self.alive

    configs = FakeConfigStore(_generation(1, CapabilityDeltaKind.EXPANSION))
    configs.target_item = _generation(2, CapabilityDeltaKind.EXPANSION)
    configs.current_item = configs.target_item
    runtime = FakeRuntimeStore()
    mcp = Mcp()
    tunnel = Tunnel()
    execution_worker = ExecutionWorker()
    runtime_path = tmp_path / "runtime.json"
    runtime_path.write_text(
        '{"pid":999,"process_identity":"' + "f" * 64 + '","active_generation":2}',
        encoding="utf-8",
    )
    profile_store = MemoryTunnelProfileStore()
    supervisor = RuntimeSupervisor(
        store=runtime,
        restart_history=_NullRestartHistory(),
        configs=configs,
        locks=Locks(),
        control=Server(),
        mcp_control=mcp,
        tunnel=tunnel,
        profile_store=profile_store,
        clock=FixedClock("2026-07-13T00:00:00+00:00"),
        ids=SequenceIdGenerator(("supervisor", "health")),
        processes=Processes(),
        mcp_runtime_path=runtime_path,
        log_path=tmp_path / "runtime.log",
        execution_worker=execution_worker,
        execution_worker_log_path=tmp_path / "execution-worker.log",
        health_timeout_seconds=0.2,
        max_restarts=0,
    )
    mcp.on_health = supervisor._stop.set
    profile = TunnelProfile("a" * 64, "repoforge", "tunnel-client", "1.0", ("rf", "serve"))

    assert (
        supervisor.run(
            generation=2,
            profile=profile,
            tool_surface_hash="b" * 64,
            environment={},
        )
        == 0
    )
    assert configs.activations == [2]
    assert configs.active_item.generation == 2
    assert tunnel.terminated
    assert tunnel.initialize_calls == 1
    assert execution_worker.started_generations == [2]
    assert execution_worker.terminated
    assert profile_store.value == profile.fingerprint
    assert profile_store.commits == [profile.fingerprint]


def test_supervisor_health_command_fails_when_child_is_not_healthy() -> None:
    from contextlib import nullcontext

    from repoforge.application.runtime.supervisor import RuntimeSupervisor

    class Locks:
        def lock(self, name: str, *, timeout_seconds=None, metadata=None):
            del name, timeout_seconds, metadata
            return nullcontext()

    class Server:
        def start(self, handler):
            del handler

        def close(self):
            pass

        def is_serving(self) -> bool:
            return True

        def is_healthy(self) -> bool:
            return True

        def serving_diagnostic(self) -> str:
            return "fake control server"

    class Never:
        def request(self, request, *, timeout_seconds=10.0):
            del request, timeout_seconds
            raise AssertionError

    class Tunnel:
        def is_alive(self, child):
            del child
            return False

    class Processes:
        def identity(self, pid: int) -> str | None:
            del pid
            return "f" * 64

    # A fresh (non-stale) snapshot recording the child as unhealthy -- this test's own
    # name asserts on "child is not healthy", not on an absent/stale observation, which
    # is now its own distinct, correctly-labeled outcome (#448 Signature C).
    record = replace(
        _record(RuntimePhase.DEGRADED, 1),
        health=(("tunnel_child", False, "managed child process exited"),),
        health_observed_at="2026-07-13T00:00:00+00:00",
    )
    runtime = FakeRuntimeStore(record)
    supervisor = RuntimeSupervisor(
        store=runtime,
        restart_history=_NullRestartHistory(),
        configs=FakeConfigStore(_generation(1, CapabilityDeltaKind.EXPANSION)),
        locks=Locks(),
        control=Server(),
        mcp_control=Never(),
        tunnel=Tunnel(),  # type: ignore[arg-type]
        profile_store=MemoryTunnelProfileStore(),
        clock=FixedClock("2026-07-13T00:00:00+00:00"),
        ids=SequenceIdGenerator(("id",)),
        processes=Processes(),
        mcp_runtime_path=Path("/missing"),
        log_path=Path("/missing"),
    )
    response = supervisor._control_handler(ControlRequest(1, ControlCommand.HEALTH, "c"))
    assert not response.ok
    assert response.error_code == "RUNTIME_UNHEALTHY"


def test_supervisor_health_command_reports_stale_when_never_observed() -> None:
    """No health observation on record yet (e.g. watchdog hasn't run) must be reported
    as an honest `stale`/`unknown` outcome, never silently conflated with a definite
    'this was observed and is unhealthy' RUNTIME_UNHEALTHY (#448 Signature C)."""
    from contextlib import nullcontext

    from repoforge.application.runtime.supervisor import RuntimeSupervisor

    class Locks:
        def lock(self, name: str, *, timeout_seconds=None, metadata=None):
            del name, timeout_seconds, metadata
            return nullcontext()

    class Server:
        def start(self, handler):
            del handler

        def close(self):
            pass

        def is_serving(self) -> bool:
            return True

        def is_healthy(self) -> bool:
            return True

        def serving_diagnostic(self) -> str:
            return "fake control server"

    class Never:
        def request(self, request, *, timeout_seconds=10.0):
            del request, timeout_seconds
            raise AssertionError

    class Tunnel:
        def is_alive(self, child):
            del child
            return False

    class Processes:
        def identity(self, pid: int) -> str | None:
            del pid
            return "f" * 64

    runtime = FakeRuntimeStore(_record(RuntimePhase.DEGRADED, 1))
    supervisor = RuntimeSupervisor(
        store=runtime,
        restart_history=_NullRestartHistory(),
        configs=FakeConfigStore(_generation(1, CapabilityDeltaKind.EXPANSION)),
        locks=Locks(),
        control=Server(),
        mcp_control=Never(),
        tunnel=Tunnel(),  # type: ignore[arg-type]
        profile_store=MemoryTunnelProfileStore(),
        clock=FixedClock("2026-07-13T00:00:00+00:00"),
        ids=SequenceIdGenerator(("id",)),
        processes=Processes(),
        mcp_runtime_path=Path("/missing"),
        log_path=Path("/missing"),
    )
    response = supervisor._control_handler(ControlRequest(1, ControlCommand.HEALTH, "c"))
    assert not response.ok
    assert response.error_code == "HEALTH_SNAPSHOT_STALE"
    payload = dict(response.payload)
    assert payload["health_freshness"] == "unknown"
    assert payload["health_age_seconds"] is None


def test_supervisor_health_command_reports_stale_past_the_freshness_threshold() -> None:
    """A health snapshot older than `health_snapshot_stale_after_seconds` must be
    reported `stale`, never `healthy`, even if the last recorded checks were all
    passing (#448 Signature C)."""
    from contextlib import nullcontext

    from repoforge.application.runtime.supervisor import RuntimeSupervisor
    from repoforge.domain.runtime import ChildProcess

    class Locks:
        def lock(self, name: str, *, timeout_seconds=None, metadata=None):
            del name, timeout_seconds, metadata
            return nullcontext()

    class Server:
        def start(self, handler):
            del handler

        def close(self):
            pass

        def is_serving(self) -> bool:
            return True

        def is_healthy(self) -> bool:
            return True

        def serving_diagnostic(self) -> str:
            return "fake control server"

    class Never:
        def request(self, request, *, timeout_seconds=10.0):
            del request, timeout_seconds
            raise AssertionError

    class Tunnel:
        def is_alive(self, child):
            del child
            return True

    identity = "f" * 64

    class Processes:
        def identity(self, pid: int) -> str | None:
            del pid
            return identity

    record = replace(
        _record(RuntimePhase.HEALTHY, 1),
        health=(("tunnel_child", True, "managed child process is alive"),),
        # Far in the past relative to the clock below -- older than any reasonable
        # `health_snapshot_stale_after_seconds`.
        health_observed_at="2020-01-01T00:00:00+00:00",
    )
    runtime = FakeRuntimeStore(record)
    supervisor = RuntimeSupervisor(
        store=runtime,
        restart_history=_NullRestartHistory(),
        configs=FakeConfigStore(_generation(1, CapabilityDeltaKind.EXPANSION)),
        locks=Locks(),
        control=Server(),
        mcp_control=Never(),
        tunnel=Tunnel(),  # type: ignore[arg-type]
        profile_store=MemoryTunnelProfileStore(),
        clock=FixedClock("2026-07-13T00:00:00+00:00"),
        ids=SequenceIdGenerator(("id",)),
        processes=Processes(),  # type: ignore[arg-type]
        mcp_runtime_path=Path("/missing"),
        log_path=Path("/missing"),
        health_snapshot_stale_after_seconds=30.0,
    )
    supervisor._child = ChildProcess(100, identity, "2026-07-13T00:00:00+00:00")

    response = supervisor._control_handler(ControlRequest(1, ControlCommand.HEALTH, "c"))

    assert not response.ok
    assert response.error_code == "HEALTH_SNAPSHOT_STALE"
    payload = dict(response.payload)
    assert payload["health_freshness"] == "stale"
    assert payload["health_age_seconds"] > 30.0


def test_health_command_reads_the_watchdogs_last_snapshot_without_a_new_probe(
    tmp_path: Path,
) -> None:
    """External HEALTH must answer from the watchdog's last observation, never launch
    its own nested MCP round-trip (#448 Signature C): the control socket is a single
    dedicated accept thread, so a request-triggered probe can block every other caller
    behind it and manufacture a timeout that has nothing to do with real service health."""
    from contextlib import nullcontext

    from repoforge.application.runtime.supervisor import RuntimeSupervisor
    from repoforge.domain.runtime import ChildProcess

    class Locks:
        def lock(self, name: str, *, timeout_seconds=None, metadata=None):
            del name, timeout_seconds, metadata
            return nullcontext()

    class Server:
        def start(self, handler):
            del handler

        def close(self):
            pass

        def is_serving(self) -> bool:
            return True

        def is_healthy(self) -> bool:
            return True

        def serving_diagnostic(self) -> str:
            return "fake control server"

    class CountingProbe:
        """Records every call instead of raising: `_observe_health` catches and
        redacts any exception from this call into a health-check detail string,
        so a raising stub would be silently absorbed rather than failing loudly."""

        def __init__(self) -> None:
            self.call_count = 0

        def request(self, request, *, timeout_seconds=10.0):
            del request, timeout_seconds
            self.call_count += 1
            raise AssertionError("external HEALTH must not launch a new nested MCP probe")

    class Tunnel:
        def is_alive(self, child):
            del child
            return True

    identity = "f" * 64

    class Processes:
        def identity(self, pid: int) -> str | None:
            del pid
            return identity

    mcp_runtime_path = tmp_path / "mcp-runtime.json"
    mcp_runtime_path.write_text(
        json.dumps({"pid": 100, "process_identity": identity, "active_generation": 1}),
        encoding="utf-8",
    )

    record = replace(
        _record(RuntimePhase.HEALTHY, 1),
        health=(
            ("tunnel_child", True, "managed child process is alive"),
            ("tunnel_admin", True, "ok"),
            ("control_plane_response", True, "ok"),
            ("control_plane", True, "ok"),
            ("mcp_generation", True, "ok"),
            ("repository_self_check", True, "repo_list completed through MCP control"),
        ),
        health_observed_at="2026-07-13T00:00:00+00:00",
    )
    runtime = FakeRuntimeStore(record)
    probe = CountingProbe()
    supervisor = RuntimeSupervisor(
        store=runtime,
        restart_history=_NullRestartHistory(),
        configs=FakeConfigStore(_generation(1, CapabilityDeltaKind.EXPANSION)),
        locks=Locks(),
        control=Server(),
        mcp_control=probe,
        tunnel=Tunnel(),  # type: ignore[arg-type]
        profile_store=MemoryTunnelProfileStore(),
        clock=FixedClock("2026-07-13T00:00:00+00:00"),
        ids=SequenceIdGenerator(("id",)),
        processes=Processes(),  # type: ignore[arg-type]
        mcp_runtime_path=mcp_runtime_path,
        log_path=tmp_path / "runtime.log",
    )
    supervisor._child = ChildProcess(100, identity, "2026-07-13T00:00:00+00:00")

    response = supervisor._control_handler(ControlRequest(1, ControlCommand.HEALTH, "c"))

    assert probe.call_count == 0, (
        "external HEALTH triggered a fresh nested MCP probe instead of reading the "
        "watchdog's last snapshot"
    )
    assert response.ok
    payload = dict(response.payload)
    assert payload["health"] == list(record.health)
    assert payload["health_observed_at"] == record.health_observed_at


def test_incompatible_generation_uses_supervisor_restart_without_hot_reload() -> None:
    previous = _generation(1, CapabilityDeltaKind.EQUIVALENT)
    configs = FakeConfigStore(previous)
    runtime = FakeRuntimeStore(_record(RuntimePhase.HEALTHY, 1))
    launcher = FakeLauncher(runtime)
    launcher.configs = configs
    control = FakeControl(runtime)
    activator = GenerationActivator(
        configs=configs,
        runtime=runtime,
        mcp_control=control,
        supervisor_control=control,
        launcher=launcher,
        ids=SequenceIdGenerator(("correlation",)),
        clock=FixedClock("2026-07-13T00:00:00+00:00"),
        config_path=Path("/config"),
        health_timeout_seconds=0.1,
        drain_timeout_seconds=0.1,
    )

    result = activator.activate(_generation(2, CapabilityDeltaKind.INCOMPATIBLE), extra_env={})

    assert result.status == "active"
    assert ControlCommand.RELOAD not in control.commands
    assert control.commands[:2] == [ControlCommand.DRAIN, ControlCommand.SHUTDOWN]
    assert launcher.started == [2]


def test_supervisor_watchdog_restarts_a_live_but_unhealthy_tunnel(tmp_path: Path) -> None:
    from contextlib import nullcontext

    from repoforge.application.runtime.supervisor import RuntimeSupervisor
    from repoforge.domain.runtime import ChildProcess, HealthCheck, TunnelProfile

    class Locks:
        def lock(self, name: str, *, timeout_seconds=None, metadata=None):
            del name, timeout_seconds, metadata
            return nullcontext()

    class Server:
        def start(self, handler):
            self.handler = handler

        def close(self):
            pass

        def is_serving(self) -> bool:
            return True

        def is_healthy(self) -> bool:
            return True

        def serving_diagnostic(self) -> str:
            return "fake control server"

    class Processes:
        def identity(self, pid: int) -> str | None:
            return "f" * 64 if pid > 0 else None

    class Mcp:
        def request(self, request, *, timeout_seconds=10.0):
            del timeout_seconds
            if tunnel.starts == 2:
                supervisor._stop.set()
            return ControlResponse(1, True, request.correlation_id, "healthy")

    class Tunnel:
        def __init__(self) -> None:
            self.starts = 0
            self.health_calls = 0
            self.terminated = 0
            self.correlations: list[str] = []

        def initialize(self, profile, *, env):
            del profile, env

        def doctor(self, profile, *, env):
            del profile, env
            return True, "ok"

        def start(self, profile, *, env, log_path, correlation_id):
            del profile, env, log_path
            self.starts += 1
            self.health_calls = 0
            self.correlations.append(correlation_id)
            return ChildProcess(200 + self.starts, "f" * 64, "now")

        def is_alive(self, child):
            del child
            return True

        def health(self, child, *, timeout_seconds):
            del child, timeout_seconds
            self.health_calls += 1
            if self.starts == 1 and self.health_calls >= 2:
                return (HealthCheck("control_plane_response", False, "502 response path"),)
            return (HealthCheck("control_plane_response", True, "ok"),)

        def terminate(self, child, *, grace_seconds):
            del child, grace_seconds
            self.terminated += 1

    configs = FakeConfigStore(_generation(1, CapabilityDeltaKind.EXPANSION))
    configs.target_item = _generation(2, CapabilityDeltaKind.EXPANSION)
    configs.current_item = configs.target_item
    runtime = FakeRuntimeStore()
    tunnel = Tunnel()
    runtime_path = tmp_path / "runtime.json"
    runtime_path.write_text(
        '{"pid":999,"process_identity":"' + "f" * 64 + '","active_generation":2}',
        encoding="utf-8",
    )
    supervisor = RuntimeSupervisor(
        store=runtime,
        restart_history=_NullRestartHistory(),
        configs=configs,
        locks=Locks(),
        control=Server(),
        mcp_control=Mcp(),
        tunnel=tunnel,
        profile_store=MemoryTunnelProfileStore(),
        clock=FixedClock("2026-07-13T00:00:00+00:00"),
        ids=SequenceIdGenerator(tuple(f"id-{index}" for index in range(20))),
        processes=Processes(),
        mcp_runtime_path=runtime_path,
        log_path=tmp_path / "runtime.log",
        health_timeout_seconds=0.1,
        watchdog_interval_seconds=0.001,
        health_failure_threshold=2,
        max_restarts=1,
    )
    profile = TunnelProfile("a" * 64, "repoforge", "tunnel-client", "1.0", ("rf", "serve"))

    assert (
        supervisor.run(
            generation=2,
            profile=profile,
            tool_surface_hash="b" * 64,
            environment={},
        )
        == 0
    )
    assert tunnel.starts == 2
    assert tunnel.terminated >= 2
    assert runtime.record is not None
    assert tunnel.correlations == [runtime.record.correlation_id] * 2


def test_restart_backoff_includes_jitter_not_just_a_deterministic_delay() -> None:
    """Bounded restarts need backoff AND jitter (#448 Slice 5), not backoff alone.

    A remote outage (Signature B) can knock several components' tunnel-children
    unhealthy at close to the same moment; a purely deterministic delay would have
    every one of them retry in lockstep. Proven statistically, not by mocking
    `random`, since the point is variance across repeated calls at the same
    `restart_count`, not one specific value.
    """
    from repoforge.application.runtime.supervisor import _restart_backoff_seconds

    restart_count = 4  # base = min(4.0, 0.25 * 2**3) = 2.0 -- bounds are [1.0, 2.0]
    samples = [_restart_backoff_seconds(restart_count) for _ in range(50)]

    assert len(set(samples)) > 1, "every sample was identical -- no jitter is present"
    assert all(1.0 <= value <= 2.0 for value in samples), samples

    # And the overall cap is unchanged regardless of how large restart_count grows.
    capped = [_restart_backoff_seconds(50) for _ in range(20)]
    assert all(2.0 <= value <= 4.0 for value in capped), capped


def test_restart_backoff_jitter_is_injectable_for_deterministic_testing() -> None:
    """The jitter source is a parameter, not a hardcoded call into the global `random`
    module (#448 Slice 5 review): a test can assert an exact value against a scripted
    sequence instead of only statistical variance, and production still gets a real
    random source by default.
    """
    from repoforge.application.runtime.supervisor import _restart_backoff_seconds

    calls: list[tuple[float, float]] = []

    def scripted(low: float, high: float) -> float:
        calls.append((low, high))
        # Deterministic, not `random.uniform` -- proves the injected callable, not the
        # global module, produced this exact value.
        return high

    restart_count = 4  # base = min(4.0, 0.25 * 2**3) = 2.0
    delay = _restart_backoff_seconds(restart_count, jitter=scripted)

    assert calls == [(0, 1.0)]
    assert delay == 2.0  # base/2 (1.0) + jitter(0, 1.0) == 1.0 -> 2.0

    def always_zero(low: float, high: float) -> float:
        del low, high
        return 0.0

    minimal = _restart_backoff_seconds(restart_count, jitter=always_zero)
    assert minimal == 1.0  # base/2 alone -- the real minimum delay, never near-zero


def test_restart_backoff_delay_is_always_bounded_finite_and_non_negative() -> None:
    """Sanity-check the formula's arithmetic itself, not just one restart_count (#448
    Slice 5 review): across a wide sweep of restart counts, using the real default
    jitter source, every delay must be non-negative, finite, and never exceed the
    absolute cap -- a NaN, infinite, or negative delay would corrupt `time.sleep`.
    """
    import math

    from repoforge.application.runtime.supervisor import _restart_backoff_seconds

    for restart_count in range(1, 200):
        for _ in range(5):
            delay = _restart_backoff_seconds(restart_count)
            assert math.isfinite(delay), (restart_count, delay)
            assert delay >= 0.0, (restart_count, delay)
            assert delay <= 4.0, (restart_count, delay)


def test_restart_budget_exhaustion_is_a_distinctly_labeled_terminal_state(tmp_path: Path) -> None:
    """The `max_restarts`-exceeded outcome must be diagnosable AND must not let launchd
    relaunch the supervisor at all (#448 Slice 5).

    Before this fix, exhausting the restart budget wrote a FAILED record and returned 2
    -- exactly the exit `KeepAlive: {SuccessfulExit: False, Crashed: True}` relaunches,
    so a remote outage that exhausted the child's restart budget could turn into an
    unbounded respawn loop: new incarnation, same lock contention, same exhaustion,
    `return 2` again. Resident fail-closed (reusing the same `_serve_fail_closed`
    every other fail-closed path already uses) means this process never exits on its
    own at all -- proven here by observing it block past the point the old code would
    already have returned, and only then triggering the explicit stop an operator
    would issue.
    """
    from contextlib import nullcontext

    from repoforge.application.runtime.supervisor import RuntimeSupervisor
    from repoforge.domain.runtime import ChildProcess, HealthCheck, RuntimePhase, TunnelProfile

    class Locks:
        def lock(self, name: str, *, timeout_seconds=None, metadata=None):
            del name, timeout_seconds, metadata
            return nullcontext()

    class Server:
        def start(self, handler):
            self.handler = handler

        def close(self):
            pass

        def is_serving(self) -> bool:
            return True

        def is_healthy(self) -> bool:
            return True

        def serving_diagnostic(self) -> str:
            return "fake control server"

    class Processes:
        def identity(self, pid: int) -> str | None:
            return "f" * 64 if pid > 0 else None

    class Mcp:
        def request(self, request, *, timeout_seconds=10.0):
            del timeout_seconds
            return ControlResponse(1, True, request.correlation_id, "healthy")

    class Tunnel:
        def __init__(self) -> None:
            self.starts = 0
            self.health_calls = 0

        def initialize(self, profile, *, env):
            del profile, env

        def doctor(self, profile, *, env):
            del profile, env
            return True, "ok"

        def start(self, profile, *, env, log_path, correlation_id):
            del profile, env, log_path, correlation_id
            self.starts += 1
            self.health_calls = 0
            return ChildProcess(200 + self.starts, "f" * 64, "now")

        def is_alive(self, child):
            del child
            return True

        def health(self, child, *, timeout_seconds):
            del child, timeout_seconds
            self.health_calls += 1
            # Healthy at startup (so this reaches the watchdog loop as a previously-good
            # child, the RESTART_LIMIT path), then fails every watchdog tick after --
            # with max_restarts=0 the very first watchdog-observed failure exceeds budget.
            if self.health_calls == 1:
                return (HealthCheck("control_plane_response", True, "ok"),)
            return (HealthCheck("control_plane_response", False, "502 response path"),)

        def terminate(self, child, *, grace_seconds):
            del child, grace_seconds

    configs = FakeConfigStore(_generation(1, CapabilityDeltaKind.EXPANSION))
    configs.target_item = _generation(2, CapabilityDeltaKind.EXPANSION)
    configs.current_item = configs.target_item
    runtime = FakeRuntimeStore()
    tunnel = Tunnel()
    runtime_path = tmp_path / "runtime.json"
    runtime_path.write_text(
        '{"pid":999,"process_identity":"' + "f" * 64 + '","active_generation":2}',
        encoding="utf-8",
    )
    supervisor = RuntimeSupervisor(
        store=runtime,
        restart_history=_NullRestartHistory(),
        configs=configs,
        locks=Locks(),
        control=Server(),
        mcp_control=Mcp(),
        tunnel=tunnel,
        profile_store=MemoryTunnelProfileStore(),
        clock=FixedClock("2026-07-13T00:00:00+00:00"),
        ids=SequenceIdGenerator(tuple(f"id-{index}" for index in range(20))),
        processes=Processes(),
        mcp_runtime_path=runtime_path,
        log_path=tmp_path / "runtime.log",
        health_timeout_seconds=0.1,
        watchdog_interval_seconds=0.001,
        health_failure_threshold=1,
        max_restarts=0,
    )
    profile = TunnelProfile("a" * 64, "repoforge", "tunnel-client", "1.0", ("rf", "serve"))

    exit_codes: list[int] = []

    def run_supervisor() -> None:
        exit_codes.append(
            supervisor.run(
                generation=2,
                profile=profile,
                tool_surface_hash="b" * 64,
                environment={},
            )
        )

    thread = threading.Thread(target=run_supervisor, daemon=True)
    thread.start()

    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if runtime.record is not None and runtime.record.phase is RuntimePhase.FAIL_CLOSED:
            break
        time.sleep(0.01)
    else:
        raise AssertionError("restart budget exhaustion never reached FAIL_CLOSED")

    assert runtime.record.last_error_code == "RESTART_LIMIT"
    assert runtime.record.last_error_code != "SUPERVISOR_FAILURE"
    assert runtime.record.fail_closed_since is not None

    # The process must not have exited on its own: still blocked well past the point
    # the old (buggy) code would already have returned 2, and the control plane is
    # still answerable (`_serve_fail_closed`'s whole point) rather than the process
    # being gone.
    thread.join(timeout=0.3)
    assert thread.is_alive(), "the supervisor exited on its own instead of staying resident"
    assert exit_codes == []

    # Only an explicit stop (an operator's `rf runtime stop`, in reality) ends it.
    supervisor._stop.set()
    thread.join(timeout=5.0)
    assert not thread.is_alive()
    assert exit_codes == [0]


def test_repeated_launchd_relaunches_after_restart_limit_do_not_repeat_the_restart_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The durable circuit breaker #448 Slice 5 requires, proven across three
    simulated launchd relaunches, not just the first incarnation's own resident wait.

    Even though a resident fail-closed incarnation never exits on its own, this proves
    the *next* layer of defense too: if something else (an operator's force-kill, an
    OS restart) ever did end that process and launchd spawned a replacement anyway,
    the replacement must not repeat the exact restart cycle that just got exhausted --
    it inherits `fail_closed_since` from the durable record (`_durable_fail_closed`)
    and goes straight to resident fail-closed without ever calling `tunnel.start()`
    again. Three incarnations in a row prove this isn't a one-off.
    """
    from contextlib import nullcontext

    from repoforge.application.runtime.supervisor import RuntimeSupervisor
    from repoforge.domain.runtime import ChildProcess, HealthCheck, RuntimePhase, TunnelProfile

    release_sha = "deadbeef"
    monkeypatch.setenv("REPOFORGE_RUNNING_RELEASE_SHA", release_sha)

    class Locks:
        def lock(self, name: str, *, timeout_seconds=None, metadata=None):
            del name, timeout_seconds, metadata
            return nullcontext()

    class Server:
        def start(self, handler):
            self.handler = handler

        def close(self):
            pass

        def is_serving(self) -> bool:
            return True

        def is_healthy(self) -> bool:
            return True

        def serving_diagnostic(self) -> str:
            return "fake control server"

    class Processes:
        def identity(self, pid: int) -> str | None:
            return "f" * 64 if pid > 0 else None

    class Mcp:
        def request(self, request, *, timeout_seconds=10.0):
            del timeout_seconds
            return ControlResponse(1, True, request.correlation_id, "healthy")

    class Tunnel:
        """Shared across every simulated incarnation: `starts` proves whether a LATER
        incarnation ever re-attempts the restart cycle the first one exhausted."""

        def __init__(self) -> None:
            self.starts = 0
            self.health_calls = 0

        def initialize(self, profile, *, env):
            del profile, env

        def doctor(self, profile, *, env):
            del profile, env
            return True, "ok"

        def start(self, profile, *, env, log_path, correlation_id):
            del profile, env, log_path, correlation_id
            self.starts += 1
            self.health_calls = 0
            return ChildProcess(200 + self.starts, "f" * 64, "now")

        def is_alive(self, child):
            del child
            return True

        def health(self, child, *, timeout_seconds):
            del child, timeout_seconds
            self.health_calls += 1
            if self.health_calls == 1:
                return (HealthCheck("control_plane_response", True, "ok"),)
            return (HealthCheck("control_plane_response", False, "502 response path"),)

        def terminate(self, child, *, grace_seconds):
            del child, grace_seconds

    configs = FakeConfigStore(_generation(1, CapabilityDeltaKind.EXPANSION))
    configs.target_item = _generation(2, CapabilityDeltaKind.EXPANSION)
    configs.current_item = configs.target_item
    # Shared across incarnations, exactly like the real durable file on disk.
    runtime = FakeRuntimeStore()
    tunnel = Tunnel()
    runtime_path = tmp_path / "runtime.json"
    runtime_path.write_text(
        '{"pid":999,"process_identity":"' + "f" * 64 + '","active_generation":2}',
        encoding="utf-8",
    )
    profile = TunnelProfile("a" * 64, "repoforge", "tunnel-client", "1.0", ("rf", "serve"))

    def new_incarnation() -> RuntimeSupervisor:
        return RuntimeSupervisor(
            store=runtime,
            restart_history=_NullRestartHistory(),
            configs=configs,
            locks=Locks(),
            control=Server(),
            mcp_control=Mcp(),
            tunnel=tunnel,
            profile_store=MemoryTunnelProfileStore(),
            clock=FixedClock("2026-07-13T00:00:00+00:00"),
            ids=SequenceIdGenerator(tuple(f"id-{index}" for index in range(20))),
            processes=Processes(),
            mcp_runtime_path=runtime_path,
            log_path=tmp_path / "runtime.log",
            health_timeout_seconds=0.1,
            watchdog_interval_seconds=0.001,
            health_failure_threshold=1,
            max_restarts=0,
        )

    def run_until_fail_closed(supervisor: RuntimeSupervisor) -> threading.Thread:
        thread = threading.Thread(
            target=supervisor.run,
            kwargs={
                "generation": 2,
                "profile": profile,
                "tool_surface_hash": "b" * 64,
                "environment": {},
            },
            daemon=True,
        )
        thread.start()
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if runtime.record is not None and runtime.record.phase is RuntimePhase.FAIL_CLOSED:
                return thread
            time.sleep(0.01)
        raise AssertionError("incarnation never reached FAIL_CLOSED")

    # Incarnation 1: genuinely exhausts the restart budget -- one real tunnel start.
    first = new_incarnation()
    run_until_fail_closed(first)
    assert tunnel.starts == 1
    assert runtime.record.last_error_code == "RESTART_LIMIT"

    # Incarnations 2 and 3: simulate launchd relaunching anyway (e.g. a force-kill of
    # the resident process). Each must inherit the durable fail-closed latch and go
    # straight to resident fail-closed WITHOUT calling `tunnel.start()` again.
    for _ in range(2):
        supervisor = new_incarnation()
        thread = run_until_fail_closed(supervisor)
        assert tunnel.starts == 1, "a later incarnation repeated the exhausted restart cycle"
        assert runtime.record.last_error_code == "RESTART_LIMIT"
        supervisor._stop.set()
        thread.join(timeout=5.0)
        assert not thread.is_alive()

    first._stop.set()


def test_unreadable_prior_runtime_state_enters_resident_fail_closed_without_spawning(
    tmp_path: Path,
) -> None:
    """An unreadable prior runtime state is NOT equivalent to an absent one (#448
    Slice 6 review).

    The startup read of the prior `RuntimeRecord` is what proves whether a previous
    incarnation already latched a durable fail-closed state for this release
    (`_durable_fail_closed`). Before this fix, a read failure there was silently
    treated the same as "no prior record": the supervisor would proceed straight to
    preflight and could spawn the tunnel child, reopening exactly the restart cycle
    #448 Slice 5 exists to keep closed -- a genuine fail-closed latch could be sitting
    right there, just unreadable, and the code would never know. An unreadable prior
    state must fail closed itself, in memory, before ever reaching preflight or a
    tunnel-start attempt.
    """
    from contextlib import nullcontext

    from repoforge.application.runtime.supervisor import RuntimeSupervisor
    from repoforge.domain.runtime import TunnelProfile

    class _NullRestartHistory:
        def read(self):
            return None

        def record_restart(self, *, incarnation_id, reason, occurred_at, event_id):
            del incarnation_id, reason, occurred_at, event_id
            raise AssertionError("a restart must never be recorded: the tunnel never starts")

        def seed_if_missing(self, *, restarts_total, last_restart_at, incarnation_id, occurred_at):
            del restarts_total, last_restart_at, incarnation_id, occurred_at
            return None

    class _UnreadableAtStartupRuntimeStore:
        """`read()` always raises -- the exact condition that must never be treated
        as "no prior record"."""

        def __init__(self) -> None:
            self.record = None
            self.write_attempts = 0

        def read(self):
            raise OSError("simulated read failure (disk full)")

        def write(self, record):
            self.write_attempts += 1
            self.record = record

        def clear(self, *, expected_pid=None):
            del expected_pid
            self.record = None

        def peek_restart_evidence(self):
            return None

    class Locks:
        def lock(self, name, *, timeout_seconds=None, metadata=None):
            del name, timeout_seconds, metadata
            return nullcontext()

    class Server:
        def start(self, handler):
            self.handler = handler

        def close(self):
            pass

        def is_serving(self) -> bool:
            return True

        def is_healthy(self) -> bool:
            return True

        def serving_diagnostic(self) -> str:
            return "fake control server"

    class Processes:
        def identity(self, pid: int) -> str | None:
            return "f" * 64 if pid > 0 else None

    class Mcp:
        def request(self, request, *, timeout_seconds=10.0):
            del timeout_seconds
            return ControlResponse(1, True, request.correlation_id, "healthy")

    class Tunnel:
        def __init__(self) -> None:
            self.starts = 0

        def initialize(self, profile, *, env):
            del profile, env

        def doctor(self, profile, *, env):
            del profile, env
            return True, "ok"

        def start(self, profile, *, env, log_path, correlation_id):
            del profile, env, log_path, correlation_id
            self.starts += 1
            raise AssertionError(
                "the tunnel must never be started: read failure must fail closed first"
            )

        def is_alive(self, child):
            del child
            return False

        def terminate(self, child, *, grace_seconds):
            del child, grace_seconds

    configs = FakeConfigStore(_generation(1, CapabilityDeltaKind.EXPANSION))
    configs.target_item = _generation(2, CapabilityDeltaKind.EXPANSION)
    configs.current_item = configs.target_item
    runtime = _UnreadableAtStartupRuntimeStore()
    tunnel = Tunnel()
    restart_history = _NullRestartHistory()
    runtime_path = tmp_path / "runtime.json"
    runtime_path.write_text(
        '{"pid":999,"process_identity":"' + "f" * 64 + '","active_generation":2}',
        encoding="utf-8",
    )
    supervisor = RuntimeSupervisor(
        store=runtime,
        restart_history=restart_history,
        configs=configs,
        locks=Locks(),
        control=Server(),
        mcp_control=Mcp(),
        tunnel=tunnel,
        profile_store=MemoryTunnelProfileStore(),
        clock=FixedClock("2026-07-13T00:00:00+00:00"),
        ids=SequenceIdGenerator(tuple(f"id-{index}" for index in range(20))),
        processes=Processes(),
        mcp_runtime_path=runtime_path,
        log_path=tmp_path / "runtime.log",
        max_restarts=3,
    )
    profile = TunnelProfile("a" * 64, "repoforge", "tunnel-client", "1.0", ("rf", "serve"))

    exit_codes: list[int] = []

    def run_supervisor() -> None:
        exit_codes.append(
            supervisor.run(
                generation=2,
                profile=profile,
                tool_surface_hash="b" * 64,
                environment={},
            )
        )

    thread = threading.Thread(target=run_supervisor, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if supervisor._fail_closed_override is not None:
            break
        time.sleep(0.01)
    else:
        raise AssertionError("never reached the in-memory fail-closed latch")

    # The tunnel was never started -- the read failure fails closed before preflight
    # or the run loop ever gets a chance to spawn anything.
    thread.join(timeout=0.3)
    assert thread.is_alive(), "the supervisor exited on its own instead of staying resident"
    assert exit_codes == []
    assert tunnel.starts == 0

    ping = supervisor._control_handler(ControlRequest(1, ControlCommand.PING, "ping-1"))
    ping_payload = dict(ping.payload)
    assert ping_payload["record"] == "fail_closed"
    assert ping_payload["last_error_code"] == "RUNTIME_STATE_READ_FAILED"

    status = supervisor._control_handler(ControlRequest(1, ControlCommand.STATUS, "status-1"))
    status_payload = dict(status.payload)
    assert status_payload["record"] == "fail_closed"
    assert status_payload["last_error_code"] == "RUNTIME_STATE_READ_FAILED"

    supervisor._stop.set()
    thread.join(timeout=5.0)
    assert not thread.is_alive()
    assert exit_codes == [0]


def test_generation_adoption_read_failure_never_reports_healthy_or_changes_generation(
    tmp_path: Path,
) -> None:
    """A read failure during generation adoption (#448 Slice 6 review) must not be
    treated as "no committed generation" and silently folded into a healthy report.

    `_adopt_committed_runtime_generation` keeps the caller's own generation on a read
    failure -- never adopts an unproven target -- but the failure itself must remain
    visible: `_observe_health` folds `_generation_read_failed_detail` into its check
    set, so the watchdog reports unhealthy while a read is failing, rather than
    silently continuing to report healthy as if the read had simply found nothing.
    Exercised directly against the two methods rather than the full `run()` loop,
    since this is a narrow, self-contained invariant between them.
    """
    from repoforge.application.runtime.supervisor import RuntimeSupervisor
    from repoforge.domain.runtime import ChildProcess

    class _SwitchableReadStore:
        def __init__(self) -> None:
            self.fail = False

        def read(self):
            if self.fail:
                raise OSError("simulated read failure (disk full)")
            return None

        def write(self, record):
            del record

        def clear(self, *, expected_pid=None):
            del expected_pid

        def peek_restart_evidence(self):
            return None

    class Configs:
        def active(self):
            return None

        def clear_activation_target(self, *, expected_generation=None):
            del expected_generation

    class Control:
        def start(self, handler):
            del handler

        def close(self):
            pass

        def is_serving(self) -> bool:
            return True

        def is_healthy(self) -> bool:
            return True

        def serving_diagnostic(self) -> str:
            return "fake control server"

    class Processes:
        def identity(self, pid: int) -> str | None:
            return "f" * 64 if pid > 0 else None

    class Mcp:
        def request(self, request, *, timeout_seconds=10.0):
            del timeout_seconds
            return ControlResponse(1, True, request.correlation_id, "healthy")

    class Tunnel:
        def is_alive(self, child):
            del child
            return True

    store = _SwitchableReadStore()
    runtime_path = tmp_path / "runtime.json"
    runtime_path.write_text(
        '{"pid":999,"process_identity":"' + "f" * 64 + '","active_generation":5}',
        encoding="utf-8",
    )
    supervisor = RuntimeSupervisor(
        store=store,
        restart_history=_NullRestartHistory(),
        configs=Configs(),
        locks=object(),  # never used by the two methods under test
        control=Control(),
        mcp_control=Mcp(),
        tunnel=Tunnel(),
        profile_store=MemoryTunnelProfileStore(),
        clock=FixedClock("2026-07-13T00:00:00+00:00"),
        ids=SequenceIdGenerator(tuple(f"id-{index}" for index in range(5))),
        processes=Processes(),
        mcp_runtime_path=runtime_path,
        log_path=tmp_path / "runtime.log",
    )
    child = ChildProcess(1000, "f" * 64, "now")

    # Baseline: the read succeeds, so adoption and health both behave normally --
    # proves the new check is genuinely conditional, not always-failing.
    assert supervisor._adopt_committed_runtime_generation(5) == 5
    assert supervisor._generation_read_failed_detail is None
    healthy, checks = supervisor._observe_health(5, child)
    assert healthy is True
    assert not any(name == "runtime_state_read" for name, _ok, _detail in checks)

    # The read starts failing -- generation must stay exactly what it was (never
    # falsely adopted, never falsely cleared), and health must flip to unhealthy.
    store.fail = True
    generation = supervisor._adopt_committed_runtime_generation(5)
    assert generation == 5
    assert supervisor._generation_read_failed_detail is not None

    healthy, checks = supervisor._observe_health(5, child)
    assert healthy is False
    read_checks = [
        (name, ok, detail) for name, ok, detail in checks if name == "runtime_state_read"
    ]
    assert len(read_checks) == 1
    assert read_checks[0][1] is False


def test_generation_state_read_failure_never_restarts_a_healthy_tunnel(tmp_path: Path) -> None:
    """A generation-adoption read failure must never restart a genuinely healthy
    tunnel child (#448 Slice 6 review).

    Before this fix, the watchdog treated every `observed_ok=False` identically:
    once the `runtime_state_read` check (folded in by the generation-adoption fix)
    made `observed_ok` false, it counted toward `consecutive_health_failures` exactly
    like a real tunnel/control-plane failure would, and once that streak crossed
    `health_failure_threshold` the watchdog terminated a tunnel child that was never
    actually unhealthy -- turning a transient store-read hiccup into an unnecessary
    restart, and with enough of them, into `RESTART_LIMIT`/fail-closed. The store here
    stays unreadable for far longer than `health_failure_threshold` watchdog ticks, on
    purpose, to prove the streak genuinely never accumulates toward a kill.
    """
    from contextlib import nullcontext

    from repoforge.application.runtime.supervisor import RuntimeSupervisor
    from repoforge.domain.runtime import ChildProcess, HealthCheck, TunnelProfile

    class _StoreFailingOnlyDuringWatchdog:
        """Reads succeed at startup (so the supervisor reaches the tunnel-start
        attempt at all) but start failing the moment the tunnel actually starts --
        exactly the shape of a store going bad partway through a healthy incarnation's
        life, not at boot."""

        def __init__(self) -> None:
            self.record = None
            self.should_fail = False
            self.read_calls = 0

        def read(self):
            self.read_calls += 1
            if self.should_fail:
                raise OSError("simulated read failure (disk full)")
            return self.record

        def write(self, record):
            self.record = record

        def clear(self, *, expected_pid=None):
            del expected_pid
            self.record = None

        def peek_restart_evidence(self):
            return None

    class Locks:
        def lock(self, name, *, timeout_seconds=None, metadata=None):
            del name, timeout_seconds, metadata
            return nullcontext()

    class Server:
        def start(self, handler):
            self.handler = handler

        def close(self):
            pass

        def is_serving(self) -> bool:
            return True

        def is_healthy(self) -> bool:
            return True

        def serving_diagnostic(self) -> str:
            return "fake control server"

    class Processes:
        def identity(self, pid: int) -> str | None:
            return "f" * 64 if pid > 0 else None

    class Mcp:
        def request(self, request, *, timeout_seconds=10.0):
            del timeout_seconds
            return ControlResponse(1, True, request.correlation_id, "healthy")

    class Tunnel:
        def __init__(self, store: _StoreFailingOnlyDuringWatchdog) -> None:
            self._store = store
            self.starts = 0
            self.terminated = 0

        def initialize(self, profile, *, env):
            del profile, env

        def doctor(self, profile, *, env):
            del profile, env
            return True, "ok"

        def start(self, profile, *, env, log_path, correlation_id):
            del profile, env, log_path, correlation_id
            self.starts += 1
            self._store.should_fail = True
            return ChildProcess(200 + self.starts, "f" * 64, "now")

        def is_alive(self, child):
            del child
            return True

        def health(self, child, *, timeout_seconds):
            del child, timeout_seconds
            # Genuinely, continuously healthy -- the only thing wrong is the store.
            return (HealthCheck("control_plane_response", True, "ok"),)

        def terminate(self, child, *, grace_seconds):
            del child, grace_seconds
            self.terminated += 1

    configs = FakeConfigStore(_generation(1, CapabilityDeltaKind.EXPANSION))
    configs.target_item = _generation(2, CapabilityDeltaKind.EXPANSION)
    configs.current_item = configs.target_item
    store = _StoreFailingOnlyDuringWatchdog()
    tunnel = Tunnel(store)
    restart_history = _NullRestartHistory()
    runtime_path = tmp_path / "runtime.json"
    runtime_path.write_text(
        '{"pid":999,"process_identity":"' + "f" * 64 + '","active_generation":2}',
        encoding="utf-8",
    )
    supervisor = RuntimeSupervisor(
        store=store,
        restart_history=restart_history,
        configs=configs,
        locks=Locks(),
        control=Server(),
        mcp_control=Mcp(),
        tunnel=tunnel,
        profile_store=MemoryTunnelProfileStore(),
        clock=FixedClock("2026-07-13T00:00:00+00:00"),
        ids=SequenceIdGenerator(tuple(f"id-{index}" for index in range(20))),
        processes=Processes(),
        mcp_runtime_path=runtime_path,
        log_path=tmp_path / "runtime.log",
        health_timeout_seconds=0.1,
        watchdog_interval_seconds=0.01,
        health_failure_threshold=2,
        max_restarts=3,
    )
    profile = TunnelProfile("a" * 64, "repoforge", "tunnel-client", "1.0", ("rf", "serve"))

    exit_codes: list[int] = []

    def run_supervisor() -> None:
        exit_codes.append(
            supervisor.run(
                generation=2,
                profile=profile,
                tool_surface_hash="b" * 64,
                environment={},
            )
        )

    thread = threading.Thread(target=run_supervisor, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5.0
    # Well past `health_failure_threshold` (2) watchdog ticks with the store failing.
    while time.monotonic() < deadline:
        if store.should_fail and store.read_calls >= 20:
            break
        time.sleep(0.01)
    else:
        raise AssertionError("never accumulated enough failing reads to prove the point")

    assert thread.is_alive(), "the supervisor exited instead of staying resident"
    assert exit_codes == []
    # The tunnel was started exactly once and never terminated or restarted, despite
    # dozens of failing reads -- proving the restart streak never counted them.
    assert tunnel.starts == 1
    assert tunnel.terminated == 0
    assert restart_history.record is None

    supervisor._stop.set()
    thread.join(timeout=5.0)
    assert not thread.is_alive()
    assert exit_codes == [0]


def test_generation_state_read_failure_clears_after_a_successful_read(tmp_path: Path) -> None:
    """A transient runtime-state read failure must self-clear once a read succeeds
    again (#448 Slice 6 review): otherwise a single momentary I/O blip would degrade
    the runtime for the rest of its life, never able to report healthy again.
    """
    from contextlib import nullcontext

    from repoforge.application.runtime.supervisor import RuntimeSupervisor
    from repoforge.domain.runtime import ChildProcess, HealthCheck, TunnelProfile

    class _StoreFailingOnce:
        """Fails exactly the first read after the tunnel starts, then recovers."""

        def __init__(self) -> None:
            self.record = None
            self.armed = False
            self.failed_once = False

        def read(self):
            if self.armed and not self.failed_once:
                self.failed_once = True
                raise OSError("simulated transient read failure (EAGAIN)")
            return self.record

        def write(self, record):
            self.record = record

        def clear(self, *, expected_pid=None):
            del expected_pid
            self.record = None

        def peek_restart_evidence(self):
            return None

    class Locks:
        def lock(self, name, *, timeout_seconds=None, metadata=None):
            del name, timeout_seconds, metadata
            return nullcontext()

    class Server:
        def start(self, handler):
            self.handler = handler

        def close(self):
            pass

        def is_serving(self) -> bool:
            return True

        def is_healthy(self) -> bool:
            return True

        def serving_diagnostic(self) -> str:
            return "fake control server"

    class Processes:
        def identity(self, pid: int) -> str | None:
            return "f" * 64 if pid > 0 else None

    class Mcp:
        def request(self, request, *, timeout_seconds=10.0):
            del timeout_seconds
            return ControlResponse(1, True, request.correlation_id, "healthy")

    class Tunnel:
        def __init__(self, store: _StoreFailingOnce) -> None:
            self._store = store
            self.starts = 0
            self.terminated = 0

        def initialize(self, profile, *, env):
            del profile, env

        def doctor(self, profile, *, env):
            del profile, env
            return True, "ok"

        def start(self, profile, *, env, log_path, correlation_id):
            del profile, env, log_path, correlation_id
            self.starts += 1
            self._store.armed = True
            return ChildProcess(200 + self.starts, "f" * 64, "now")

        def is_alive(self, child):
            del child
            return True

        def health(self, child, *, timeout_seconds):
            del child, timeout_seconds
            return (HealthCheck("control_plane_response", True, "ok"),)

        def terminate(self, child, *, grace_seconds):
            del child, grace_seconds
            self.terminated += 1

    configs = FakeConfigStore(_generation(1, CapabilityDeltaKind.EXPANSION))
    configs.target_item = _generation(2, CapabilityDeltaKind.EXPANSION)
    configs.current_item = configs.target_item
    store = _StoreFailingOnce()
    tunnel = Tunnel(store)
    restart_history = _NullRestartHistory()
    runtime_path = tmp_path / "runtime.json"
    runtime_path.write_text(
        '{"pid":999,"process_identity":"' + "f" * 64 + '","active_generation":2}',
        encoding="utf-8",
    )
    supervisor = RuntimeSupervisor(
        store=store,
        restart_history=restart_history,
        configs=configs,
        locks=Locks(),
        control=Server(),
        mcp_control=Mcp(),
        tunnel=tunnel,
        profile_store=MemoryTunnelProfileStore(),
        clock=FixedClock("2026-07-13T00:00:00+00:00"),
        ids=SequenceIdGenerator(tuple(f"id-{index}" for index in range(20))),
        processes=Processes(),
        mcp_runtime_path=runtime_path,
        log_path=tmp_path / "runtime.log",
        health_timeout_seconds=0.1,
        watchdog_interval_seconds=0.01,
        health_failure_threshold=2,
        max_restarts=3,
    )
    profile = TunnelProfile("a" * 64, "repoforge", "tunnel-client", "1.0", ("rf", "serve"))

    exit_codes: list[int] = []

    def run_supervisor() -> None:
        exit_codes.append(
            supervisor.run(
                generation=2,
                profile=profile,
                tool_surface_hash="b" * 64,
                environment={},
            )
        )

    thread = threading.Thread(target=run_supervisor, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5.0
    # Wait for the FULL cycle: the transient failure must actually have happened, and
    # `_generation_read_failed_detail` must since have been cleared by a later
    # successful read -- checking the record's phase alone would race ahead of this,
    # since the very first HEALTHY write happens right after tunnel start, before the
    # watchdog loop (and so before the armed failure) ever runs at all.
    while time.monotonic() < deadline:
        if store.failed_once and supervisor._generation_read_failed_detail is None:
            break
        time.sleep(0.01)
    else:
        raise AssertionError("never recovered after the transient read failure")

    assert thread.is_alive()
    assert exit_codes == []
    assert supervisor._generation_read_failed_detail is None
    assert tunnel.starts == 1
    assert tunnel.terminated == 0
    assert restart_history.record is None

    supervisor._stop.set()
    thread.join(timeout=5.0)
    assert not thread.is_alive()
    assert exit_codes == [0]


def test_a_restart_ledger_write_failure_fails_closed_instead_of_crash_looping(
    tmp_path: Path,
) -> None:
    """A durable-ledger write failure right before a restart decision must fail closed,
    not fall through to the generic top-level handler (#448 Slice 4).

    Before this fix, an exception from `record_restart()` (disk full, a corrupted
    sibling lock file, ...) was unhandled at its call site: it propagated to the
    top-level `except Exception`, which terminates any live child, writes a bare
    `FAILED` record, and returns 2 -- exactly the nonzero exit that lets launchd's
    `KeepAlive` relaunch the supervisor, which is the exact crash-loop pattern #448
    Slice 5 closed for restart-BUDGET exhaustion but not for a failure in the ledger
    write itself. With plenty of restart budget left (`max_restarts=3`, only one
    attempted restart), the fix must still go resident fail-closed on the very first
    ledger-write failure rather than retrying or exiting.
    """
    from contextlib import nullcontext

    from repoforge.application.runtime.supervisor import RuntimeSupervisor
    from repoforge.domain.runtime import RuntimePhase, TunnelProfile

    class _FailingRestartHistory:
        def __init__(self) -> None:
            self.record = None
            self.record_restart_calls = 0

        def read(self):
            return self.record

        def record_restart(self, *, incarnation_id, reason, occurred_at, event_id):
            del incarnation_id, reason, occurred_at, event_id
            self.record_restart_calls += 1
            raise OSError("simulated ledger write failure (disk full)")

        def seed_if_missing(self, *, restarts_total, last_restart_at, incarnation_id, occurred_at):
            del restarts_total, last_restart_at, incarnation_id, occurred_at
            return self.record

    class Locks:
        def lock(self, name, *, timeout_seconds=None, metadata=None):
            del name, timeout_seconds, metadata
            return nullcontext()

    class Server:
        def start(self, handler):
            self.handler = handler

        def close(self):
            pass

        def is_serving(self) -> bool:
            return True

        def is_healthy(self) -> bool:
            return True

        def serving_diagnostic(self) -> str:
            return "fake control server"

    class Processes:
        def identity(self, pid: int) -> str | None:
            return "f" * 64 if pid > 0 else None

    class Mcp:
        def request(self, request, *, timeout_seconds=10.0):
            del timeout_seconds
            return ControlResponse(1, True, request.correlation_id, "healthy")

    class Tunnel:
        def __init__(self) -> None:
            self.starts = 0

        def initialize(self, profile, *, env):
            del profile, env

        def doctor(self, profile, *, env):
            del profile, env
            return True, "ok"

        def start(self, profile, *, env, log_path, correlation_id):
            del profile, env, log_path, correlation_id
            self.starts += 1
            raise RuntimeError("tunnel failed to start")

        def is_alive(self, child):
            del child
            return False

        def terminate(self, child, *, grace_seconds):
            del child, grace_seconds

    configs = FakeConfigStore(_generation(1, CapabilityDeltaKind.EXPANSION))
    configs.target_item = _generation(2, CapabilityDeltaKind.EXPANSION)
    configs.current_item = configs.target_item
    runtime = FakeRuntimeStore()
    tunnel = Tunnel()
    restart_history = _FailingRestartHistory()
    runtime_path = tmp_path / "runtime.json"
    runtime_path.write_text(
        '{"pid":999,"process_identity":"' + "f" * 64 + '","active_generation":2}',
        encoding="utf-8",
    )
    supervisor = RuntimeSupervisor(
        store=runtime,
        restart_history=restart_history,
        configs=configs,
        locks=Locks(),
        control=Server(),
        mcp_control=Mcp(),
        tunnel=tunnel,
        profile_store=MemoryTunnelProfileStore(),
        clock=FixedClock("2026-07-13T00:00:00+00:00"),
        ids=SequenceIdGenerator(tuple(f"id-{index}" for index in range(20))),
        processes=Processes(),
        mcp_runtime_path=runtime_path,
        log_path=tmp_path / "runtime.log",
        # Plenty of restart budget left -- the fail-closed outcome below must come
        # from the ledger-write failure itself, not from budget exhaustion.
        max_restarts=3,
    )
    profile = TunnelProfile("a" * 64, "repoforge", "tunnel-client", "1.0", ("rf", "serve"))

    exit_codes: list[int] = []

    def run_supervisor() -> None:
        exit_codes.append(
            supervisor.run(
                generation=2,
                profile=profile,
                tool_surface_hash="b" * 64,
                environment={},
            )
        )

    thread = threading.Thread(target=run_supervisor, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if runtime.record is not None and runtime.record.phase is RuntimePhase.FAIL_CLOSED:
            break
        time.sleep(0.01)
    else:
        raise AssertionError("ledger write failure never reached FAIL_CLOSED")

    assert runtime.record.last_error_code == "RESTART_LEDGER_WRITE_FAILED"
    assert runtime.record.last_error_code != "SUPERVISOR_FAILURE"
    assert runtime.record.fail_closed_since is not None
    # No evidence fabricated: the ledger write never succeeded, so the record must not
    # claim a confirmed restart that was never durably recorded.
    assert runtime.record.restarts_total == 0
    # Exactly one restart was attempted -- proving the failure was caught at the first
    # opportunity rather than after retrying past the still-available budget.
    assert tunnel.starts == 1
    assert restart_history.record_restart_calls == 1

    thread.join(timeout=0.3)
    assert thread.is_alive(), "the supervisor exited on its own instead of staying resident"
    assert exit_codes == []

    supervisor._stop.set()
    thread.join(timeout=5.0)
    assert not thread.is_alive()
    assert exit_codes == [0]


def test_fail_closed_state_write_failure_does_not_reenter_launchd_exit_loop(
    tmp_path: Path,
) -> None:
    """A second layer of failure -- the durable FAIL_CLOSED record write ALSO failing,
    on top of the restart-ledger write that already failed -- must still stay
    resident, never propagate to the top-level handler's `return 2` (#448 Slice 6
    review).

    Before this fix, `_record_restart_or_fail_closed`'s own `self._store.write(...)`
    call for the FAIL_CLOSED record was unguarded: if the persistent store was
    unavailable for the same reason the ledger write just failed (e.g. the whole state
    directory's disk is out of space), that second exception propagated straight past
    `_serve_fail_closed` to the generic top-level handler, which returns 2 -- exactly
    the nonzero exit that lets launchd relaunch the supervisor into the identical
    double failure. Every side effect in the failure path must be independently
    best-effort so persistence trouble can never prevent staying resident.
    """
    from contextlib import nullcontext

    from repoforge.application.runtime.supervisor import RuntimeSupervisor
    from repoforge.domain.runtime import RuntimePhase, TunnelProfile

    class _FailingRestartHistory:
        def __init__(self) -> None:
            self.record = None
            self.record_restart_calls = 0

        def read(self):
            return self.record

        def record_restart(self, *, incarnation_id, reason, occurred_at, event_id):
            del incarnation_id, reason, occurred_at, event_id
            self.record_restart_calls += 1
            raise OSError("simulated ledger write failure (disk full)")

        def seed_if_missing(self, *, restarts_total, last_restart_at, incarnation_id, occurred_at):
            del restarts_total, last_restart_at, incarnation_id, occurred_at
            return self.record

    class _AlsoFailingOnFailClosedRuntimeStore:
        """Writes normally, EXCEPT a FAIL_CLOSED record also hits the same disk-full
        condition that broke the ledger -- the scenario this test exists to prove."""

        def __init__(self) -> None:
            self.record = None
            self.fail_closed_write_attempts = 0

        def read(self):
            return self.record

        def write(self, record):
            if record.phase is RuntimePhase.FAIL_CLOSED:
                self.fail_closed_write_attempts += 1
                raise OSError("simulated runtime store write failure (disk full)")
            self.record = record

        def clear(self, *, expected_pid=None):
            del expected_pid
            self.record = None

        def peek_restart_evidence(self):
            return None

    class Locks:
        def lock(self, name, *, timeout_seconds=None, metadata=None):
            del name, timeout_seconds, metadata
            return nullcontext()

    class Server:
        def start(self, handler):
            self.handler = handler

        def close(self):
            pass

        def is_serving(self) -> bool:
            return True

        def is_healthy(self) -> bool:
            return True

        def serving_diagnostic(self) -> str:
            return "fake control server"

    class Processes:
        def identity(self, pid: int) -> str | None:
            return "f" * 64 if pid > 0 else None

    class Mcp:
        def request(self, request, *, timeout_seconds=10.0):
            del timeout_seconds
            return ControlResponse(1, True, request.correlation_id, "healthy")

    class Tunnel:
        def __init__(self) -> None:
            self.starts = 0

        def initialize(self, profile, *, env):
            del profile, env

        def doctor(self, profile, *, env):
            del profile, env
            return True, "ok"

        def start(self, profile, *, env, log_path, correlation_id):
            del profile, env, log_path, correlation_id
            self.starts += 1
            raise RuntimeError("tunnel failed to start")

        def is_alive(self, child):
            del child
            return False

        def terminate(self, child, *, grace_seconds):
            del child, grace_seconds

    configs = FakeConfigStore(_generation(1, CapabilityDeltaKind.EXPANSION))
    configs.target_item = _generation(2, CapabilityDeltaKind.EXPANSION)
    configs.current_item = configs.target_item
    runtime = _AlsoFailingOnFailClosedRuntimeStore()
    tunnel = Tunnel()
    restart_history = _FailingRestartHistory()
    runtime_path = tmp_path / "runtime.json"
    runtime_path.write_text(
        '{"pid":999,"process_identity":"' + "f" * 64 + '","active_generation":2}',
        encoding="utf-8",
    )
    supervisor = RuntimeSupervisor(
        store=runtime,
        restart_history=restart_history,
        configs=configs,
        locks=Locks(),
        control=Server(),
        mcp_control=Mcp(),
        tunnel=tunnel,
        profile_store=MemoryTunnelProfileStore(),
        clock=FixedClock("2026-07-13T00:00:00+00:00"),
        ids=SequenceIdGenerator(tuple(f"id-{index}" for index in range(20))),
        processes=Processes(),
        mcp_runtime_path=runtime_path,
        log_path=tmp_path / "runtime.log",
        max_restarts=3,
    )
    profile = TunnelProfile("a" * 64, "repoforge", "tunnel-client", "1.0", ("rf", "serve"))

    exit_codes: list[int] = []

    def run_supervisor() -> None:
        exit_codes.append(
            supervisor.run(
                generation=2,
                profile=profile,
                tool_surface_hash="b" * 64,
                environment={},
            )
        )

    thread = threading.Thread(target=run_supervisor, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if restart_history.record_restart_calls >= 1 and runtime.fail_closed_write_attempts >= 1:
            break
        time.sleep(0.01)
    else:
        raise AssertionError("never reached the ledger-write + store-write double failure")

    # Neither write ever lands, so `runtime.record` never becomes readable as
    # FAIL_CLOSED -- the only observable proof left is that the process stayed
    # resident instead of exiting, and never retried past the first failure.
    thread.join(timeout=0.3)
    assert thread.is_alive(), "the supervisor exited on its own instead of staying resident"
    assert exit_codes == [], "run() must never return while both writes are failing"
    assert tunnel.starts == 1, "a double persistence failure must not trigger a respawn"
    assert restart_history.record_restart_calls == 1
    assert runtime.fail_closed_write_attempts == 1

    supervisor._stop.set()
    thread.join(timeout=5.0)
    assert not thread.is_alive()
    assert exit_codes == [0]


def test_in_memory_fail_closed_remains_observable_and_exits_cleanly_when_store_is_unwritable(
    tmp_path: Path,
) -> None:
    """The full emergency path (#448 Slice 6 review): the restart-history ledger AND
    the runtime store's WRITES are both unavailable, not just the ledger alone.

    `read()` here always succeeds (returns `None`, i.e. "no record yet") rather than
    raising: an unreadable store at startup is a materially different, worse
    condition -- it can hide a genuine prior fail-closed latch -- and is covered by
    its own dedicated test
    (`test_unreadable_prior_runtime_state_enters_resident_fail_closed_without_spawning`)
    with its own distinct `error_code`. This test isolates the write-only failure: the
    in-memory `_fail_closed_override` latch is what the control plane answers from --
    it is consulted before `self._store.read()` is ever called at all (`override or
    read()` short-circuits), so a store whose writes are all failing can never make
    the true fail-closed reason unreadable. And at shutdown, persisting the terminal
    STOPPED transition fails too (the same broken store), yet `run()` still returns 0
    -- proving a persistence failure during an *explicit* shutdown can never turn into
    the nonzero exit that would let a simulated launchd relaunch this incarnation.
    """
    from contextlib import nullcontext

    from repoforge.application.runtime.supervisor import RuntimeSupervisor
    from repoforge.domain.runtime import RuntimePhase, TunnelProfile

    class _FailingRestartHistory:
        def __init__(self) -> None:
            self.record = None
            self.record_restart_calls = 0

        def read(self):
            return self.record

        def record_restart(self, *, incarnation_id, reason, occurred_at, event_id):
            del incarnation_id, reason, occurred_at, event_id
            self.record_restart_calls += 1
            raise OSError("simulated ledger write failure (disk full)")

        def seed_if_missing(self, *, restarts_total, last_restart_at, incarnation_id, occurred_at):
            del restarts_total, last_restart_at, incarnation_id, occurred_at
            return self.record

    class _WriteOnlyBrokenRuntimeStore:
        """`read()` always succeeds with "no record" -- this test is isolating a
        pure write failure, not the separately-tested unreadable-state case. `write()`
        still accepts the ordinary STARTING write before each tunnel-start attempt
        (so the run loop can reach that attempt at all, exactly as it would on a
        real, otherwise-healthy incarnation) but fails every other write -- in
        particular the FAIL_CLOSED and STOPPED writes this test exists to prove
        survive a broken store."""

        def __init__(self) -> None:
            self.write_attempts = 0
            self.non_starting_write_attempts = 0

        def read(self):
            return None

        def write(self, record):
            self.write_attempts += 1
            if record.phase is RuntimePhase.STARTING:
                return
            self.non_starting_write_attempts += 1
            raise OSError("simulated write failure (disk full)")

        def clear(self, *, expected_pid=None):
            del expected_pid

        def peek_restart_evidence(self):
            return None

    class Locks:
        def lock(self, name, *, timeout_seconds=None, metadata=None):
            del name, timeout_seconds, metadata
            return nullcontext()

    class Server:
        def start(self, handler):
            self.handler = handler

        def close(self):
            pass

        def is_serving(self) -> bool:
            return True

        def is_healthy(self) -> bool:
            return True

        def serving_diagnostic(self) -> str:
            return "fake control server"

    class Processes:
        def identity(self, pid: int) -> str | None:
            return "f" * 64 if pid > 0 else None

    class Mcp:
        def request(self, request, *, timeout_seconds=10.0):
            del timeout_seconds
            return ControlResponse(1, True, request.correlation_id, "healthy")

    class Tunnel:
        def __init__(self) -> None:
            self.starts = 0

        def initialize(self, profile, *, env):
            del profile, env

        def doctor(self, profile, *, env):
            del profile, env
            return True, "ok"

        def start(self, profile, *, env, log_path, correlation_id):
            del profile, env, log_path, correlation_id
            self.starts += 1
            raise RuntimeError("tunnel failed to start")

        def is_alive(self, child):
            del child
            return False

        def terminate(self, child, *, grace_seconds):
            del child, grace_seconds

    configs = FakeConfigStore(_generation(1, CapabilityDeltaKind.EXPANSION))
    configs.target_item = _generation(2, CapabilityDeltaKind.EXPANSION)
    configs.current_item = configs.target_item
    runtime = _WriteOnlyBrokenRuntimeStore()
    tunnel = Tunnel()
    restart_history = _FailingRestartHistory()
    runtime_path = tmp_path / "runtime.json"
    runtime_path.write_text(
        '{"pid":999,"process_identity":"' + "f" * 64 + '","active_generation":2}',
        encoding="utf-8",
    )
    supervisor = RuntimeSupervisor(
        store=runtime,
        restart_history=restart_history,
        configs=configs,
        locks=Locks(),
        control=Server(),
        mcp_control=Mcp(),
        tunnel=tunnel,
        profile_store=MemoryTunnelProfileStore(),
        clock=FixedClock("2026-07-13T00:00:00+00:00"),
        ids=SequenceIdGenerator(tuple(f"id-{index}" for index in range(20))),
        processes=Processes(),
        mcp_runtime_path=runtime_path,
        log_path=tmp_path / "runtime.log",
        max_restarts=3,
    )
    profile = TunnelProfile("a" * 64, "repoforge", "tunnel-client", "1.0", ("rf", "serve"))

    exit_codes: list[int] = []

    def run_supervisor() -> None:
        exit_codes.append(
            supervisor.run(
                generation=2,
                profile=profile,
                tool_surface_hash="b" * 64,
                environment={},
            )
        )

    thread = threading.Thread(target=run_supervisor, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if (
            restart_history.record_restart_calls >= 1
            and runtime.non_starting_write_attempts >= 1
            and supervisor._fail_closed_override is not None
        ):
            break
        time.sleep(0.01)
    else:
        raise AssertionError("never reached the in-memory fail-closed latch")

    # 1 & 2: resident, no respawn -- same shape as the write-failure test above.
    thread.join(timeout=0.3)
    assert thread.is_alive(), "the supervisor exited on its own instead of staying resident"
    assert exit_codes == []
    assert tunnel.starts == 1

    # 3 & 4: the control plane is still answerable, and the override -- not a
    # (permanently broken) store read -- is what it answers from.
    ping = supervisor._control_handler(ControlRequest(1, ControlCommand.PING, "ping-1"))
    assert ping.ok is True
    ping_payload = dict(ping.payload)
    assert ping_payload["record"] == "fail_closed"
    assert ping_payload["last_error_code"] == "RESTART_LEDGER_WRITE_FAILED"

    status = supervisor._control_handler(ControlRequest(1, ControlCommand.STATUS, "status-1"))
    status_payload = dict(status.payload)
    assert status_payload["record"] == "fail_closed"
    assert status_payload["last_error_code"] == "RESTART_LEDGER_WRITE_FAILED"

    # 5, 6 & 7: explicit shutdown unblocks it, and `run()` still returns 0 even
    # though persisting STOPPED also fails -- a simulated launchd therefore never
    # gets the nonzero exit it would need to relaunch this incarnation.
    writes_before_stop = runtime.non_starting_write_attempts
    supervisor._stop.set()
    thread.join(timeout=5.0)
    assert not thread.is_alive()
    assert exit_codes == [0]
    assert runtime.non_starting_write_attempts > writes_before_stop, (
        "shutdown must still attempt to persist STOPPED, even though it will fail"
    )


def test_execution_worker_restarts_when_generation_changes_or_process_dies(tmp_path) -> None:
    from repoforge.application.runtime.supervisor import RuntimeSupervisor
    from repoforge.domain.runtime import ChildProcess

    class Worker:
        def __init__(self):
            self.started = []
            self.alive = set()
            self.terminated = []

        def start(self, generation, *, env, log_path, correlation_id):
            del env, log_path, correlation_id
            self.started.append(generation)
            child = ChildProcess(400 + len(self.started), f"{generation:064x}", "now")
            self.alive.add(child.pid)
            return child

        def is_alive(self, child):
            return child.pid in self.alive

        def terminate(self, child, *, grace_seconds):
            del grace_seconds
            self.terminated.append(child.pid)
            self.alive.discard(child.pid)

    worker = Worker()
    supervisor = RuntimeSupervisor(
        store=object(),
        restart_history=_NullRestartHistory(),
        configs=object(),
        locks=object(),
        control=object(),
        mcp_control=object(),
        tunnel=object(),
        profile_store=object(),
        clock=FixedClock("2026-07-27T00:00:00+00:00"),
        ids=SequenceIdGenerator(("worker",)),
        processes=object(),
        mcp_runtime_path=tmp_path / "mcp.json",
        log_path=tmp_path / "runtime.log",
        execution_worker=worker,
        execution_worker_log_path=tmp_path / "worker.log",
    )

    supervisor._ensure_execution_worker(12, environment={}, correlation_id="first")
    first_pid = supervisor._execution_child.pid
    supervisor._ensure_execution_worker(12, environment={}, correlation_id="same")
    supervisor._ensure_execution_worker(13, environment={}, correlation_id="generation")
    second_pid = supervisor._execution_child.pid
    worker.alive.discard(second_pid)
    supervisor._ensure_execution_worker(13, environment={}, correlation_id="dead")

    assert worker.started == [12, 13, 13]
    assert worker.terminated == [first_pid]


# ------------------------------- tunnel MCP connection lifetime (configurable, opt-in)


def test_run_argv_omits_the_ttl_flag_by_default() -> None:
    """The default must change nothing. tunnel-client recycles the MCP transport on its own
    schedule (10m at the time of writing) and shuts the client down on expiry rather than
    reconnecting, so every expiry is a window where the connector answers 502. That is
    worth being able to tune, but choosing a value for every installation is not ours."""
    from repoforge.adapters.runtime.tunnel_cli import _run_argv
    from repoforge.domain.runtime import TunnelProfile

    profile = TunnelProfile("a" * 64, "repoforge", "tunnel-client", "1.0", ("rf", "serve"))

    assert _run_argv("tunnel-client", profile) == [
        "tunnel-client",
        "run",
        "--profile",
        "repoforge",
    ]


def test_run_argv_passes_a_configured_ttl_as_a_duration() -> None:
    from repoforge.adapters.runtime.tunnel_cli import _run_argv
    from repoforge.domain.runtime import TunnelProfile

    profile = TunnelProfile("a" * 64, "repoforge", "tunnel-client", "1.0", ("rf", "serve"), 3_600)

    assert _run_argv("tunnel-client", profile)[-1] == "--mcp.connection-max-ttl=3600s"


def test_the_ttl_participates_in_the_profile_fingerprint() -> None:
    """Changing it must re-initialise the tunnel profile, or the running client keeps the
    old lifetime while the reviewed configuration claims the new one."""
    from repoforge.domain.runtime import TunnelProfile

    base = TunnelProfile("a" * 64, "repoforge", "tunnel-client", "1.0", ("rf", "serve"))
    tuned = TunnelProfile("a" * 64, "repoforge", "tunnel-client", "1.0", ("rf", "serve"), 3_600)

    assert base.fingerprint != tuned.fingerprint


def test_the_ttl_is_read_from_the_source_config_that_the_runtime_actually_parses() -> None:
    """The layer that made the first attempt unreachable.

    `[server]` is rendered into a reviewed generation from the PREVIOUS resolved document,
    not from source, so a `[server]` edit never reaches a running runtime -- deliberately,
    since that table carries capability grants. `[tunnel]` is re-read from source text on
    every runtime start, which is why the connection lifetime belongs there. A test that
    only exercised argv construction passed while the setting could not be set at all.
    """
    from repoforge.application.configuration.source import parse_source

    source = parse_source(
        "\nversion = 2\n"
        '[tunnel]\nid = "tunnel_x"\nmcp_connection_max_ttl_seconds = 3600\n'
        '[[repo]]\nid = "demo"\npath = "/tmp/demo"\n'
    )

    assert source.mcp_connection_max_ttl_seconds == 3600


def test_the_ttl_defaults_to_unset_when_the_source_omits_it() -> None:
    from repoforge.application.configuration.source import parse_source

    source = parse_source(
        '\nversion = 2\n[tunnel]\nid = "tunnel_x"\n[[repo]]\nid = "demo"\npath = "/tmp/demo"\n'
    )

    assert source.mcp_connection_max_ttl_seconds is None


def test_a_non_positive_ttl_in_source_is_refused() -> None:
    from repoforge.application.configuration.source import parse_source

    with pytest.raises(ValueError, match="mcp_connection_max_ttl_seconds"):
        parse_source(
            "\nversion = 2\n"
            '[tunnel]\nid = "tunnel_x"\nmcp_connection_max_ttl_seconds = 0\n'
            '[[repo]]\nid = "demo"\npath = "/tmp/demo"\n'
        )


def test_a_non_positive_ttl_is_refused() -> None:
    from repoforge.domain.runtime import TunnelProfile

    with pytest.raises(ValueError):
        TunnelProfile("a" * 64, "repoforge", "tunnel-client", "1.0", ("rf", "serve"), 0)
