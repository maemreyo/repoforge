"""Long-lived tunnel supervisor with bounded restart and health-gated startup."""

from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import signal
import sys
import threading
import time
from collections.abc import Iterator
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
from ...ports.execution_worker import ExecutionWorkerClient
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
        execution_worker: ExecutionWorkerClient | None = None,
        execution_worker_log_path: Path | None = None,
        health_timeout_seconds: float = 30.0,
        max_restarts: int = 3,
        watchdog_interval_seconds: float = 2.0,
        health_failure_threshold: int = 3,
        stable_health_reset_seconds: float = 60.0,
        single_instance_wait_seconds: float = 45.0,
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
        self._execution_worker = execution_worker
        self._execution_worker_log_path = execution_worker_log_path
        if execution_worker is not None and execution_worker_log_path is None:
            raise ValueError("execution_worker_log_path is required with execution_worker")
        if (
            health_timeout_seconds <= 0
            or max_restarts < 0
            or watchdog_interval_seconds <= 0
            or health_failure_threshold <= 0
            or stable_health_reset_seconds <= 0
            or single_instance_wait_seconds < 0
        ):
            raise ValueError("Runtime health and restart bounds must be positive")
        self._health_timeout = health_timeout_seconds
        self._single_instance_wait = single_instance_wait_seconds
        self._max_restarts = max_restarts
        self._watchdog_interval = watchdog_interval_seconds
        self._health_failure_threshold = health_failure_threshold
        self._stable_health_reset = stable_health_reset_seconds
        self._stop = threading.Event()
        self._child: ChildProcess | None = None
        self._execution_child: ChildProcess | None = None
        self._execution_generation: int | None = None

    def _ensure_execution_worker(
        self,
        generation: int,
        *,
        environment: dict[str, str],
        correlation_id: str,
    ) -> None:
        if self._execution_worker is None:
            return
        assert self._execution_worker_log_path is not None
        child = self._execution_child
        if (
            child is not None
            and self._execution_generation == generation
            and self._execution_worker.is_alive(child)
        ):
            return
        if child is not None and self._execution_worker.is_alive(child):
            self._execution_worker.terminate(child, grace_seconds=3)
        self._execution_child = None
        self._execution_generation = None
        self._execution_child = self._execution_worker.start(
            generation,
            env=environment,
            log_path=self._execution_worker_log_path,
            correlation_id=correlation_id,
        )
        self._execution_generation = generation

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
        restarts_total: int = 0,
        last_restart_at: str | None = None,
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
            # Captured by the launcher before exec; never re-derived here.
            running_release_sha=os.environ.get("REPOFORGE_RUNNING_RELEASE_SHA") or None,
            health_observed_at=now if health else None,
            consecutive_health_failures=consecutive_health_failures,
            restarts_total=restarts_total,
            last_restart_at=last_restart_at,
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
        # First, because a runtime that cannot answer control requests is not healthy no
        # matter what else is true. This record is written by the watchdog loop, which
        # survives the control loop, so without this check a supervisor whose control plane
        # had died kept publishing `phase: healthy` while every read needing the socket
        # timed out -- and the recovery commands, which go through that same socket, could
        # not run either (#322).
        serving = self._control.is_serving()
        checks.append(HealthCheck("control_plane", serving, self._control.serving_diagnostic()))
        if self._execution_worker is not None:
            worker_alive = bool(
                self._execution_child and self._execution_worker.is_alive(self._execution_child)
            )
            checks.append(
                HealthCheck(
                    "execution_worker",
                    worker_alive,
                    (
                        "isolated execution worker is alive"
                        if worker_alive
                        else "isolated execution worker exited"
                    ),
                )
            )
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
        execution_worker_alive = bool(
            self._execution_worker
            and self._execution_child
            and self._execution_worker.is_alive(self._execution_child)
        )
        payload: dict[str, object] = {
            "record": record.phase.value if record else "stopped",
            "active_generation": record.active_generation if record else None,
            "accepted_generation": record.accepted_generation if record else None,
            "child_alive": child_alive,
            "execution_worker_alive": execution_worker_alive,
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

    @contextlib.contextmanager
    def _single_instance_lock(self, correlation_id: str) -> Iterator[None]:
        """Take the single-instance lock, waiting a bounded interval for a handoff.

        An activation replaces this process: the incoming supervisor starts while the
        outgoing one is still shutting down and still holds this lock. Taking it with no
        wait therefore made every handoff a race the incoming process usually lost, which
        failed activations that were otherwise fine. Waiting is bounded and never
        unlimited -- a lock that is genuinely held by a supervisor which is not leaving is
        reported as a typed timeout, because two live supervisors is the exact state this
        lock exists to prevent.
        """
        entered = False
        try:
            with self._locks.lock(
                "runtime-single-instance",
                timeout_seconds=self._single_instance_wait,
                metadata={"correlation_id": correlation_id},
            ):
                entered = True
                yield
        except ConfigError as exc:
            if entered or "LOCK_TIMEOUT" not in str(exc):
                raise
            raise ConfigError(
                "RUNTIME_SUPERVISOR_HANDOFF_TIMEOUT: another supervisor still holds "
                f"'runtime-single-instance' after {self._single_instance_wait:g}s "
                f"({self._single_instance_holder()}). Stop it with `rf runtime stop` "
                "before starting this one; two live supervisors are never started."
            ) from exc

    def _single_instance_holder(self) -> str:
        """Describe the current lock holder from the lock file, for the timeout message."""
        try:
            path = self._locks.path_for("runtime-single-instance")
            payload = json.loads(path.read_text(encoding="utf-8") or "{}")
        except (AttributeError, OSError, ValueError):
            return "holder unknown"
        if not isinstance(payload, dict) or not payload.get("pid"):
            return "holder unknown"
        return f"holder pid {payload['pid']}"

    def run(
        self,
        *,
        generation: int,
        profile: TunnelProfile,
        tool_surface_hash: str,
        environment: dict[str, str],
    ) -> int:
        correlation_id = self._ids.new_hex(24)
        with self._single_instance_lock(correlation_id):
            self._control.start(self._control_handler)
            previous_handlers: dict[signal.Signals, object] = {}

            def stop_handler(_signum: int, _frame: object) -> None:
                self._stop.set()

            for signum in (signal.SIGTERM, signal.SIGINT):
                previous_handlers[signal.Signals(signum)] = signal.signal(signum, stop_handler)
            restart_count = 0
            # Carried across supervisor lifetimes: a supervisor that itself restarted would
            # otherwise republish `0 restarts` and erase the history an operator is reading
            # the record to find.
            prior_record = self._store.read()
            restarts_total = prior_record.restarts_total if prior_record is not None else 0
            last_restart_at: str | None = (
                prior_record.last_restart_at if prior_record is not None else None
            )
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

                if self._execution_worker is not None:
                    try:
                        self._ensure_execution_worker(
                            generation,
                            environment=environment,
                            correlation_id=correlation_id,
                        )
                    except Exception as exc:
                        self._store.write(
                            self._record(
                                RuntimePhase.FAILED,
                                accepted_generation=generation,
                                active_generation=None,
                                profile=profile,
                                tool_surface_hash=tool_surface_hash,
                                correlation_id=correlation_id,
                                child=None,
                                error_code="EXECUTION_WORKER_START_FAILED",
                                error=redact_text(f"{type(exc).__name__}: {exc}"),
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
                            restarts_total=restarts_total,
                            last_restart_at=last_restart_at,
                        )
                    )
                    try:
                        child = self._tunnel.start(
                            profile,
                            env=environment,
                            log_path=self._log_path,
                            correlation_id=correlation_id,
                        )
                    except Exception as exc:
                        restart_count += 1
                        # Evidence, not policy: unlike restart_count these are never reset,
                        # so an outage is still visible once health settles again.
                        restarts_total += 1
                        last_restart_at = self._clock.now_iso()
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
                                    restarts_total=restarts_total,
                                    last_restart_at=last_restart_at,
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
                        # Evidence, not policy: unlike restart_count these are never reset,
                        # so an outage is still visible once health settles again.
                        restarts_total += 1
                        last_restart_at = self._clock.now_iso()
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
                                    restarts_total=restarts_total,
                                    last_restart_at=last_restart_at,
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
                                restarts_total=restarts_total,
                                last_restart_at=last_restart_at,
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
                            restarts_total=restarts_total,
                            last_restart_at=last_restart_at,
                            health=health,
                        )
                    )

                    consecutive_health_failures = 0
                    stable_since = time.monotonic()
                    while not self._stop.is_set() and self._tunnel.is_alive(child):
                        time.sleep(self._watchdog_interval)
                        generation = self._adopt_committed_runtime_generation(generation)
                        with contextlib.suppress(Exception):
                            self._ensure_execution_worker(
                                generation,
                                environment=environment,
                                correlation_id=correlation_id,
                            )
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
                                        restarts_total=restarts_total,
                                        last_restart_at=last_restart_at,
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
                    # Evidence, not policy: unlike restart_count these are never reset,
                    # so an outage is still visible once health settles again.
                    restarts_total += 1
                    last_restart_at = self._clock.now_iso()
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
                                restarts_total=restarts_total,
                                last_restart_at=last_restart_at,
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
                                restarts_total=restarts_total,
                                last_restart_at=last_restart_at,
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
                if (
                    self._execution_worker is not None
                    and self._execution_child is not None
                    and self._execution_worker.is_alive(self._execution_child)
                ):
                    self._execution_worker.terminate(self._execution_child, grace_seconds=15)
                self._execution_child = None
                self._execution_generation = None
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
                if (
                    self._execution_worker is not None
                    and self._execution_child is not None
                    and self._execution_worker.is_alive(self._execution_child)
                ):
                    self._execution_worker.terminate(self._execution_child, grace_seconds=3)
                self._execution_child = None
                self._execution_generation = None
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
                            restarts_total=restarts_total,
                            last_restart_at=last_restart_at,
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
