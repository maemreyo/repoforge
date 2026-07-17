"""Long-lived tunnel supervisor with bounded restart and health-gated startup."""

from __future__ import annotations

import contextlib
import importlib.util
import os
import signal
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

from repoforge import __version__

from ...domain.errors import ConfigError
from ...domain.redaction import redact_text
from ...domain.runtime import (
    RUNTIME_CONTROL_PROTOCOL_VERSION,
    ChildProcess,
    ControlCommand,
    ControlRequest,
    ControlResponse,
    HealthCheck,
    RuntimePhase,
    RuntimeRecord,
    TunnelProfile,
    transition,
)
from ...ports.clock import Clock
from ...ports.configuration import ConfigurationStore
from ...ports.ids import IdGenerator
from ...ports.locking import LockManager
from ...ports.process import ProcessInspector
from ...ports.runtime_control import RuntimeControlClient, RuntimeControlServer, RuntimeStore
from ...ports.tunnel import TunnelClient, TunnelProfileStore


def _install_origin() -> str | None:
    spec = importlib.util.find_spec("repoforge")
    origin = spec.origin if spec is not None else None
    if not origin:
        return None
    normalized = origin.replace("\\", "/")
    if "/site-packages/" in normalized:
        return "wheel"
    if "/src/repoforge/" in normalized:
        return "source"
    return "environment"


class RuntimeSupervisor:
    PROTOCOL_VERSION = RUNTIME_CONTROL_PROTOCOL_VERSION

    def __init__(
        self,
        *,
        store: RuntimeStore,
        configs: ConfigurationStore,
        locks: LockManager,
        control: RuntimeControlServer,
        mcp_control: RuntimeControlClient,
        tunnel: TunnelClient,
        profile_store: TunnelProfileStore,
        clock: Clock,
        ids: IdGenerator,
        processes: ProcessInspector,
        mcp_runtime_path: Path,
        log_path: Path,
        health_timeout_seconds: float = 30.0,
        max_restarts: int = 3,
        watchdog_interval_seconds: float = 2.0,
        health_failure_threshold: int = 3,
        stable_health_reset_seconds: float = 60.0,
    ) -> None:
        self._store = store
        self._configs = configs
        self._locks = locks
        self._control = control
        self._mcp_control = mcp_control
        self._tunnel = tunnel
        self._profile_store = profile_store
        self._clock = clock
        self._ids = ids
        self._processes = processes
        self._mcp_runtime_path = mcp_runtime_path
        self._log_path = log_path
        if (
            health_timeout_seconds <= 0
            or max_restarts < 0
            or watchdog_interval_seconds <= 0
            or health_failure_threshold <= 0
            or stable_health_reset_seconds <= 0
        ):
            raise ValueError("Runtime health and restart bounds must be positive")
        self._health_timeout = health_timeout_seconds
        self._max_restarts = max_restarts
        self._watchdog_interval = watchdog_interval_seconds
        self._health_failure_threshold = health_failure_threshold
        self._stable_health_reset = stable_health_reset_seconds
        self._stop = threading.Event()
        self._child: ChildProcess | None = None

    def _clear_target(self, generation: int) -> None:
        with contextlib.suppress(ConfigError):
            self._configs.clear_activation_target(expected_generation=generation)

    def _adopt_committed_runtime_generation(self, fallback: int) -> int:
        """Adopt a hot-reloaded generation only when disk and runtime state agree."""
        active = self._configs.active()
        record = self._store.read()
        if (
            active is not None
            and record is not None
            and record.active_generation == active.generation
            and record.accepted_generation == active.generation
        ):
            return active.generation
        return fallback

    def _mcp_generation(self) -> int | None:
        if not self._mcp_runtime_path.is_file():
            return None
        try:
            import json

            raw = json.loads(self._mcp_runtime_path.read_text(encoding="utf-8"))
            pid = raw.get("pid")
            identity = raw.get("process_identity")
            generation = raw.get("active_generation")
            if (
                isinstance(pid, int)
                and isinstance(identity, str)
                and self._processes.identity(pid) == identity
                and isinstance(generation, int)
            ):
                return generation
        except (OSError, ValueError, TypeError):
            return None
        return None

    def _record(
        self,
        phase: RuntimePhase,
        *,
        accepted_generation: int,
        active_generation: int | None,
        profile: TunnelProfile,
        tool_surface_hash: str,
        correlation_id: str,
        child: ChildProcess | None,
        restart_count: int = 0,
        error_code: str | None = None,
        error: str | None = None,
        health: tuple[tuple[str, bool, str], ...] = (),
        consecutive_health_failures: int = 0,
    ) -> RuntimeRecord:
        pid = os.getpid()
        identity = self._processes.identity(pid)
        if identity is None:
            raise ConfigError("Cannot determine supervisor process identity")
        now = self._clock.now_iso()
        return RuntimeRecord(
            protocol_version=self.PROTOCOL_VERSION,
            phase=phase,
            pid=pid,
            process_identity=identity,
            active_generation=active_generation,
            accepted_generation=accepted_generation,
            tunnel_profile=profile.profile,
            tunnel_profile_fingerprint=profile.fingerprint,
            tool_surface_hash=tool_surface_hash,
            started_at=now,
            updated_at=now,
            correlation_id=correlation_id,
            child_pid=child.pid if child else None,
            child_process_identity=child.process_identity if child else None,
            restart_count=restart_count,
            last_error_code=error_code,
            last_error=error,
            health=health,
            package_version=__version__,
            executable=sys.executable,
            install_origin=_install_origin(),
            health_observed_at=now if health else None,
            consecutive_health_failures=consecutive_health_failures,
        )

    def _tunnel_health(self, child: ChildProcess) -> tuple[HealthCheck, ...]:
        probe = getattr(self._tunnel, "health", None)
        if callable(probe):
            try:
                checks = tuple(probe(child, timeout_seconds=1.0))
                if checks:
                    return checks
            except Exception as exc:
                return (
                    HealthCheck(
                        "tunnel_probe",
                        False,
                        redact_text(f"tunnel health probe failed: {type(exc).__name__}: {exc}"),
                    ),
                )
        alive = self._tunnel.is_alive(child)
        return (
            HealthCheck(
                "tunnel_child",
                alive,
                "managed child process is alive" if alive else "managed child process exited",
            ),
        )

    def _observe_health(
        self, generation: int, child: ChildProcess
    ) -> tuple[bool, tuple[tuple[str, bool, str], ...]]:
        checks = list(self._tunnel_health(child))
        mcp_generation = self._mcp_generation()
        generation_ok = mcp_generation == generation
        checks.append(
            HealthCheck(
                "mcp_generation",
                generation_ok,
                (
                    f"MCP reported generation {generation}"
                    if generation_ok
                    else f"expected generation {generation}; observed {mcp_generation}"
                ),
            )
        )
        mcp_ok = False
        mcp_detail = "MCP repository health did not pass"
        if all(check.ok for check in checks) and generation_ok:
            try:
                response = self._mcp_control.request(
                    ControlRequest(1, ControlCommand.HEALTH, self._ids.new_hex(24)),
                    timeout_seconds=2.0,
                )
                mcp_ok = response.ok and response.status == "healthy"
                mcp_detail = (
                    "repo_list completed through MCP control"
                    if mcp_ok
                    else response.message or response.status
                )
            except Exception as exc:
                mcp_detail = redact_text(f"MCP health probe failed: {type(exc).__name__}: {exc}")
        checks.append(HealthCheck("repository_self_check", mcp_ok, mcp_detail))
        legacy = tuple(check.legacy() for check in checks)
        return all(check.ok for check in checks), legacy

    def _control_handler(self, request: ControlRequest) -> ControlResponse:
        record = self._store.read()
        child_alive = bool(self._child and self._tunnel.is_alive(self._child))
        payload: dict[str, object] = {
            "record": record.phase.value if record else "stopped",
            "active_generation": record.active_generation if record else None,
            "accepted_generation": record.accepted_generation if record else None,
            "child_alive": child_alive,
            "health": list(record.health) if record else [],
            "health_observed_at": record.health_observed_at if record else None,
        }
        if request.command is ControlCommand.PING:
            return ControlResponse(
                1, True, request.correlation_id, "alive", tuple(sorted(payload.items()))
            )
        if request.command is ControlCommand.STATUS:
            return ControlResponse(
                1,
                record is not None,
                request.correlation_id,
                str(payload["record"]),
                tuple(sorted(payload.items())),
                None if record is not None else "RUNTIME_NOT_STARTED",
            )
        if request.command is ControlCommand.HEALTH:
            healthy = False
            if record and self._child and record.active_generation is not None and child_alive:
                healthy, observed = self._observe_health(record.active_generation, self._child)
                payload["health"] = list(observed)
                payload["health_observed_at"] = self._clock.now_iso()
            healthy = bool(healthy and record and record.phase is RuntimePhase.HEALTHY)
            return ControlResponse(
                1,
                healthy,
                request.correlation_id,
                "healthy" if healthy else "unhealthy",
                tuple(sorted(payload.items())),
                None if healthy else "RUNTIME_UNHEALTHY",
                None if healthy else "Supervisor or managed child is not healthy",
            )
        if request.command is ControlCommand.SHUTDOWN:
            self._stop.set()
            return ControlResponse(1, True, request.correlation_id, "stopping")
        return ControlResponse(
            1,
            False,
            request.correlation_id,
            "unsupported",
            error_code="UNSUPPORTED_CONTROL_COMMAND",
            message=request.command.value,
        )

    def _wait_healthy(
        self, generation: int, child: ChildProcess
    ) -> tuple[bool, tuple[tuple[str, bool, str], ...]]:
        deadline = time.monotonic() + self._health_timeout
        latest: tuple[tuple[str, bool, str], ...] = ()
        while time.monotonic() < deadline:
            healthy, latest = self._observe_health(generation, child)
            if healthy:
                return True, latest
            if not self._tunnel.is_alive(child):
                break
            time.sleep(0.1)
        return False, latest or (
            ("tunnel_child", False, "managed child process did not become healthy"),
        )

    def run(
        self,
        *,
        generation: int,
        profile: TunnelProfile,
        tool_surface_hash: str,
        environment: dict[str, str],
    ) -> int:
        correlation_id = self._ids.new_hex(24)
        with self._locks.lock(
            "runtime-single-instance",
            timeout_seconds=0,
            metadata={"correlation_id": correlation_id},
        ):
            self._control.start(self._control_handler)
            previous_handlers: dict[signal.Signals, object] = {}

            def stop_handler(_signum: int, _frame: object) -> None:
                self._stop.set()

            for signum in (signal.SIGTERM, signal.SIGINT):
                previous_handlers[signal.Signals(signum)] = signal.signal(signum, stop_handler)
            restart_count = 0
            try:
                try:
                    initialize_profile = self._profile_store.fingerprint() != profile.fingerprint
                    if initialize_profile:
                        self._tunnel.initialize(profile, env=environment)
                    doctor_ok, doctor_detail = self._tunnel.doctor(profile, env=environment)
                    if doctor_ok and initialize_profile:
                        self._profile_store.commit(profile)
                except Exception as exc:
                    doctor_ok, doctor_detail = False, redact_text(f"{type(exc).__name__}: {exc}")
                if not doctor_ok:
                    self._store.write(
                        self._record(
                            RuntimePhase.FAILED,
                            accepted_generation=generation,
                            active_generation=None,
                            profile=profile,
                            tool_surface_hash=tool_surface_hash,
                            correlation_id=correlation_id,
                            child=None,
                            error_code="TUNNEL_DOCTOR_FAILED",
                            error=redact_text(doctor_detail),
                        )
                    )
                    self._clear_target(generation)
                    return 2

                while not self._stop.is_set():
                    generation = self._adopt_committed_runtime_generation(generation)
                    self._store.write(
                        self._record(
                            RuntimePhase.STARTING,
                            accepted_generation=generation,
                            active_generation=None,
                            profile=profile,
                            tool_surface_hash=tool_surface_hash,
                            correlation_id=correlation_id,
                            child=None,
                            restart_count=restart_count,
                        )
                    )
                    try:
                        child = self._tunnel.start(
                            profile, env=environment, log_path=self._log_path
                        )
                    except Exception as exc:
                        restart_count += 1
                        if restart_count > self._max_restarts:
                            self._store.write(
                                self._record(
                                    RuntimePhase.FAILED,
                                    accepted_generation=generation,
                                    active_generation=None,
                                    profile=profile,
                                    tool_surface_hash=tool_surface_hash,
                                    correlation_id=correlation_id,
                                    child=None,
                                    restart_count=restart_count,
                                    error_code="TUNNEL_START_FAILED",
                                    error=redact_text(f"{type(exc).__name__}: {exc}"),
                                )
                            )
                            self._clear_target(generation)
                            return 2
                        time.sleep(min(4.0, 0.25 * (2 ** (restart_count - 1))))
                        continue
                    self._child = child
                    healthy, health = self._wait_healthy(generation, child)
                    if not healthy:
                        self._tunnel.terminate(child, grace_seconds=3)
                        self._child = None
                        restart_count += 1
                        if restart_count > self._max_restarts:
                            self._store.write(
                                self._record(
                                    RuntimePhase.FAILED,
                                    accepted_generation=generation,
                                    active_generation=None,
                                    profile=profile,
                                    tool_surface_hash=tool_surface_hash,
                                    correlation_id=correlation_id,
                                    child=None,
                                    restart_count=restart_count,
                                    error_code="STARTUP_HEALTH_FAILED",
                                    error="Tunnel/MCP did not become healthy",
                                    health=health,
                                )
                            )
                            self._clear_target(generation)
                            return 2
                        time.sleep(min(4.0, 0.25 * (2 ** (restart_count - 1))))
                        continue

                    previous = self._configs.active()
                    try:
                        if previous is None or previous.generation != generation:
                            self._configs.activate(
                                generation,
                                expected_active=previous.generation if previous else None,
                            )
                    except Exception as exc:
                        self._tunnel.terminate(child, grace_seconds=3)
                        self._child = None
                        self._store.write(
                            self._record(
                                RuntimePhase.FAILED,
                                accepted_generation=generation,
                                active_generation=None,
                                profile=profile,
                                tool_surface_hash=tool_surface_hash,
                                correlation_id=correlation_id,
                                child=None,
                                restart_count=restart_count,
                                error_code="ACTIVE_POINTER_COMMIT_FAILED",
                                error=redact_text(f"{type(exc).__name__}: {exc}"),
                                health=health,
                            )
                        )
                        self._clear_target(generation)
                        return 2
                    self._store.write(
                        self._record(
                            RuntimePhase.HEALTHY,
                            accepted_generation=generation,
                            active_generation=generation,
                            profile=profile,
                            tool_surface_hash=tool_surface_hash,
                            correlation_id=correlation_id,
                            child=child,
                            restart_count=restart_count,
                            health=health,
                        )
                    )

                    consecutive_health_failures = 0
                    stable_since = time.monotonic()
                    while not self._stop.is_set() and self._tunnel.is_alive(child):
                        time.sleep(self._watchdog_interval)
                        generation = self._adopt_committed_runtime_generation(generation)
                        observed_ok, observed_health = self._observe_health(generation, child)
                        current = self._store.read()
                        if observed_ok:
                            consecutive_health_failures = 0
                            if time.monotonic() - stable_since >= self._stable_health_reset:
                                restart_count = 0
                            if current is not None and (
                                current.phase is RuntimePhase.DEGRADED
                                or current.health != observed_health
                                or current.restart_count != restart_count
                            ):
                                self._store.write(
                                    replace(
                                        current,
                                        phase=RuntimePhase.HEALTHY,
                                        active_generation=generation,
                                        accepted_generation=generation,
                                        restart_count=restart_count,
                                        health=observed_health,
                                        health_observed_at=self._clock.now_iso(),
                                        consecutive_health_failures=0,
                                        last_error_code=None,
                                        last_error=None,
                                        updated_at=self._clock.now_iso(),
                                    )
                                )
                            continue
                        consecutive_health_failures += 1
                        stable_since = time.monotonic()
                        if current is not None:
                            self._store.write(
                                replace(
                                    current,
                                    phase=RuntimePhase.DEGRADED,
                                    health=observed_health,
                                    health_observed_at=self._clock.now_iso(),
                                    consecutive_health_failures=consecutive_health_failures,
                                    last_error_code="WATCHDOG_HEALTH_DEGRADED",
                                    last_error="A live tunnel child failed active runtime health probes",
                                    updated_at=self._clock.now_iso(),
                                )
                            )
                        if consecutive_health_failures < self._health_failure_threshold:
                            continue
                        self._tunnel.terminate(child, grace_seconds=3)
                        break
                    if self._stop.is_set():
                        break
                    self._child = None
                    generation = self._adopt_committed_runtime_generation(generation)
                    restart_count += 1
                    if restart_count > self._max_restarts:
                        self._store.write(
                            self._record(
                                RuntimePhase.FAILED,
                                accepted_generation=generation,
                                active_generation=generation,
                                profile=profile,
                                tool_surface_hash=tool_surface_hash,
                                correlation_id=correlation_id,
                                child=None,
                                restart_count=restart_count,
                                error_code="RESTART_LIMIT",
                                error="Tunnel child exceeded bounded restart policy",
                            )
                        )
                        self._clear_target(generation)
                        return 2
                    try:
                        doctor_ok, doctor_detail = self._tunnel.doctor(profile, env=environment)
                    except Exception as exc:
                        doctor_ok, doctor_detail = (
                            False,
                            redact_text(f"{type(exc).__name__}: {exc}"),
                        )
                    if not doctor_ok:
                        self._store.write(
                            self._record(
                                RuntimePhase.FAILED,
                                accepted_generation=generation,
                                active_generation=generation,
                                profile=profile,
                                tool_surface_hash=tool_surface_hash,
                                correlation_id=correlation_id,
                                child=None,
                                restart_count=restart_count,
                                error_code="NON_RETRYABLE_DOCTOR_FAILURE",
                                error=redact_text(doctor_detail),
                            )
                        )
                        self._clear_target(generation)
                        return 2
                    time.sleep(min(4.0, 0.25 * (2 ** (restart_count - 1))))

                current = self._store.read()
                if current and current.phase not in {RuntimePhase.STOPPED, RuntimePhase.FAILED}:
                    self._store.write(
                        transition(
                            current,
                            RuntimePhase.STOPPING,
                            updated_at=self._clock.now_iso(),
                            correlation_id=correlation_id,
                        )
                    )
                if self._child and self._tunnel.is_alive(self._child):
                    self._tunnel.terminate(self._child, grace_seconds=15)
                self._child = None
                current = self._store.read()
                if current:
                    self._store.write(
                        replace(
                            current,
                            phase=RuntimePhase.STOPPED,
                            pid=None,
                            process_identity=None,
                            child_pid=None,
                            child_process_identity=None,
                            active_generation=None,
                            updated_at=self._clock.now_iso(),
                        )
                    )
                return 0
            except Exception as exc:
                if self._child and self._tunnel.is_alive(self._child):
                    self._tunnel.terminate(self._child, grace_seconds=3)
                self._child = None
                with contextlib.suppress(Exception):
                    self._store.write(
                        self._record(
                            RuntimePhase.FAILED,
                            accepted_generation=generation,
                            active_generation=None,
                            profile=profile,
                            tool_surface_hash=tool_surface_hash,
                            correlation_id=correlation_id,
                            child=None,
                            restart_count=restart_count,
                            error_code="SUPERVISOR_FAILURE",
                            error=redact_text(f"{type(exc).__name__}: {exc}"),
                        )
                    )
                self._clear_target(generation)
                return 2
            finally:
                self._control.close()
                for signum, handler in previous_handlers.items():
                    signal.signal(signum, handler)  # type: ignore[arg-type]
