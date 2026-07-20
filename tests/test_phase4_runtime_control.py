from __future__ import annotations

import os
import threading
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

        def start(self, profile, *, env, log_path):
            del profile, env, log_path
            return ChildProcess(222, "f" * 64, "now")

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
    runtime_path = tmp_path / "runtime.json"
    runtime_path.write_text(
        '{"pid":999,"process_identity":"' + "f" * 64 + '","active_generation":2}',
        encoding="utf-8",
    )
    profile_store = MemoryTunnelProfileStore()
    supervisor = RuntimeSupervisor(
        store=runtime,
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

        def initialize(self, profile, *, env):
            del profile, env

        def doctor(self, profile, *, env):
            del profile, env
            return True, "ok"

        def start(self, profile, *, env, log_path):
            del profile, env, log_path
            self.starts += 1
            self.health_calls = 0
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
