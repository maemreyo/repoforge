"""Health-gated generation activation with bounded drain and safe rollback policy."""

from __future__ import annotations

import contextlib
import time
from dataclasses import dataclass, replace
from pathlib import Path

from ...domain.config_generation import CapabilityDeltaKind, ConfigGeneration
from ...domain.errors import ConfigError
from ...domain.redaction import redact_text
from ...domain.runtime import (
    RUNTIME_CONTROL_PROTOCOL_VERSION,
    ControlCommand,
    ControlRequest,
    ControlResponse,
    RuntimePhase,
    RuntimeRecord,
    transition,
)
from ...ports.clock import Clock
from ...ports.configuration import ConfigurationStore
from ...ports.ids import IdGenerator
from ...ports.runtime_control import RuntimeControlClient, RuntimeLauncher, RuntimeStore


@dataclass(frozen=True, slots=True)
class ActivationResult:
    status: str
    config_generation: int
    active_generation: int | None
    rolled_back_to: int | None
    correlation_id: str
    safe_next_action: str


class GenerationActivator:
    def __init__(
        self,
        *,
        configs: ConfigurationStore,
        runtime: RuntimeStore,
        mcp_control: RuntimeControlClient,
        supervisor_control: RuntimeControlClient,
        launcher: RuntimeLauncher,
        ids: IdGenerator,
        clock: Clock,
        config_path: Path,
        health_timeout_seconds: float = 45.0,
        drain_timeout_seconds: float = 30.0,
    ) -> None:
        self._configs = configs
        self._runtime = runtime
        self._mcp_control = mcp_control
        self._supervisor_control = supervisor_control
        self._launcher = launcher
        self._ids = ids
        self._clock = clock
        self._config_path = config_path
        self._health_timeout = health_timeout_seconds
        self._drain_timeout = drain_timeout_seconds

    def _request_response(
        self,
        client: RuntimeControlClient,
        command: ControlCommand,
        correlation_id: str,
        payload: dict[str, object] | None = None,
    ) -> ControlResponse | None:
        try:
            return client.request(
                ControlRequest(
                    RUNTIME_CONTROL_PROTOCOL_VERSION,
                    command,
                    correlation_id,
                    tuple(sorted((payload or {}).items())),
                ),
                timeout_seconds=max(5.0, self._drain_timeout),
            )
        except ConfigError:
            return None

    def _request(
        self,
        client: RuntimeControlClient,
        command: ControlCommand,
        correlation_id: str,
        payload: dict[str, object] | None = None,
    ) -> bool:
        response = self._request_response(client, command, correlation_id, payload)
        return bool(response and response.ok)

    def _wait_stopped(self, timeout: float = 20.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            record = self._runtime.read()
            if record is None or record.phase in {RuntimePhase.STOPPED, RuntimePhase.FAILED}:
                return True
            time.sleep(0.1)
        return False

    def _wait_generation(self, generation: int) -> bool:
        deadline = time.monotonic() + self._health_timeout
        while time.monotonic() < deadline:
            record = self._runtime.read()
            if (
                record
                and record.phase is RuntimePhase.HEALTHY
                and record.active_generation == generation
            ):
                return True
            if record and record.phase in {RuntimePhase.FAILED, RuntimePhase.FAIL_CLOSED}:
                return False
            time.sleep(0.1)
        return False

    def _mark_terminal_failure(
        self,
        prior: object,
        generation: ConfigGeneration,
        correlation_id: str,
        *,
        phase: RuntimePhase,
        code: str,
        message: str,
    ) -> None:
        if isinstance(prior, RuntimeRecord):
            record = replace(
                prior,
                phase=phase,
                pid=None,
                process_identity=None,
                child_pid=None,
                child_process_identity=None,
                active_generation=None,
                accepted_generation=generation.generation,
                updated_at=self._clock.now_iso(),
                correlation_id=correlation_id,
                last_error_code=code,
                last_error=message,
            )
        else:
            record = RuntimeRecord(
                protocol_version=RUNTIME_CONTROL_PROTOCOL_VERSION,
                phase=phase,
                pid=None,
                process_identity=None,
                active_generation=None,
                accepted_generation=generation.generation,
                tunnel_profile="unknown",
                tunnel_profile_fingerprint="",
                tool_surface_hash="",
                started_at=None,
                updated_at=self._clock.now_iso(),
                correlation_id=correlation_id,
                last_error_code=code,
                last_error=message,
            )
        self._runtime.write(record)

    def _transition_runtime(
        self,
        record: RuntimeRecord,
        phase: RuntimePhase,
        correlation_id: str,
        *,
        accepted_generation: int | None = None,
        error_code: str | None = None,
        error: str | None = None,
    ) -> RuntimeRecord:
        updated = transition(
            record,
            phase,
            updated_at=self._clock.now_iso(),
            correlation_id=correlation_id,
        )
        if accepted_generation is not None or error_code is not None or error is not None:
            updated = replace(
                updated,
                accepted_generation=(
                    accepted_generation
                    if accepted_generation is not None
                    else updated.accepted_generation
                ),
                last_error_code=error_code,
                last_error=redact_text(error) if error is not None else None,
            )
        self._runtime.write(updated)
        return updated

    def activate(
        self,
        generation: ConfigGeneration,
        *,
        extra_env: dict[str, str],
        wait_for_health: bool = True,
        rollback_on_failure: bool = True,
    ) -> ActivationResult:
        if not wait_for_health and rollback_on_failure:
            raise ValueError(
                "Asynchronous activation cannot guarantee automatic rollback; disable rollback "
                "explicitly when wait_for_health is false"
            )
        correlation_id = self._ids.new_hex(24)
        previous = self._configs.active()
        running = self._runtime.read()
        if (
            running
            and running.phase in {RuntimePhase.HEALTHY, RuntimePhase.DEGRADED}
            and generation.delta is not CapabilityDeltaKind.INCOMPATIBLE
        ):
            expected_active = previous.generation if previous else None
            self._configs.stage_activation(generation.generation, expected_active=expected_active)
            response = self._request_response(
                self._mcp_control,
                ControlCommand.RELOAD,
                correlation_id,
                {
                    "generation": generation.generation,
                    "expected_active": expected_active or 0,
                },
            )
            committed = self._configs.active()
            response_generation = (
                dict(response.payload).get("active_generation") if response is not None else None
            )
            if (
                committed is not None
                and committed.generation == generation.generation
                and (
                    response is None
                    or (response.ok and response_generation == generation.generation)
                )
            ):
                self._runtime.write(
                    replace(
                        running,
                        phase=RuntimePhase.HEALTHY,
                        active_generation=generation.generation,
                        accepted_generation=generation.generation,
                        updated_at=self._clock.now_iso(),
                        correlation_id=correlation_id,
                        last_error_code=None,
                        last_error=None,
                    )
                )
                return ActivationResult(
                    "hot_reloaded",
                    generation.generation,
                    generation.generation,
                    None,
                    correlation_id,
                    "The new immutable service container is active; the tunnel process was preserved.",
                )
        if running and running.phase not in {RuntimePhase.STOPPED, RuntimePhase.FAILED}:
            observable = running
            if running.phase in {RuntimePhase.HEALTHY, RuntimePhase.DEGRADED}:
                observable = self._transition_runtime(
                    running,
                    RuntimePhase.DRAINING,
                    correlation_id,
                    accepted_generation=generation.generation,
                )
            drained = self._request(
                self._mcp_control,
                ControlCommand.DRAIN,
                correlation_id,
                {"timeout_seconds": self._drain_timeout},
            )
            if not drained:
                if generation.delta is CapabilityDeltaKind.RESTRICTION:
                    gated = self._request(
                        self._mcp_control,
                        ControlCommand.FAIL_CLOSED,
                        correlation_id,
                        {"reason": "restrictive configuration pending activation"},
                    )
                    if gated:
                        self._runtime.write(
                            replace(
                                observable,
                                phase=RuntimePhase.FAIL_CLOSED,
                                accepted_generation=generation.generation,
                                updated_at=self._clock.now_iso(),
                                correlation_id=correlation_id,
                                last_error_code="RUNTIME_DRAIN_TIMEOUT",
                                last_error=(
                                    "Restrictive generation is accepted but activation is paused "
                                    "until in-flight operations finish"
                                ),
                            )
                        )
                        raise ConfigError(
                            "RUNTIME_DRAIN_TIMEOUT: runtime is fail-closed; retry activation after "
                            "in-flight operations finish"
                        )
                    # The MCP gate is unreachable, so stop external access rather than leave a
                    # removed capability remotely reachable. This forced path is explicit and
                    # identity-bound; it is never reported as a clean drain.
                    self._request(self._supervisor_control, ControlCommand.SHUTDOWN, correlation_id)
                    stopped = self._wait_stopped(timeout=5.0)
                    if not stopped:
                        stopped = self._launcher.force_stop(running, grace_seconds=5.0)
                    self._mark_terminal_failure(
                        running,
                        generation,
                        correlation_id,
                        phase=RuntimePhase.FAIL_CLOSED,
                        code="RESTRICTION_FORCED_STOP",
                        message=(
                            "MCP drain/fail-closed control was unavailable; the managed runtime "
                            "was stopped to revoke external access"
                        ),
                    )
                    raise ConfigError(
                        "RESTRICTION_FORCED_STOP: runtime was stopped because safe drain control "
                        "was unavailable; retry activation"
                    )
                self._request(self._mcp_control, ControlCommand.RESUME, correlation_id)
                if observable.phase is RuntimePhase.DRAINING:
                    self._runtime.write(
                        replace(
                            observable,
                            phase=running.phase,
                            accepted_generation=generation.generation,
                            updated_at=self._clock.now_iso(),
                            correlation_id=correlation_id,
                            last_error_code="RUNTIME_DRAIN_TIMEOUT",
                            last_error="Activation was not started because in-flight work did not drain",
                        )
                    )
                raise ConfigError(
                    "RUNTIME_DRAIN_TIMEOUT: active generation remains running; retry after "
                    "in-flight operations finish"
                )
            if observable.phase is RuntimePhase.DRAINING:
                observable = self._transition_runtime(
                    observable,
                    RuntimePhase.RELOADING,
                    correlation_id,
                    accepted_generation=generation.generation,
                )
            self._request(self._supervisor_control, ControlCommand.SHUTDOWN, correlation_id)
            if not self._wait_stopped():
                forced = self._launcher.force_stop(running, grace_seconds=5.0)
                if not forced or not self._wait_stopped(timeout=5.0):
                    self._mark_terminal_failure(
                        running,
                        generation,
                        correlation_id,
                        phase=RuntimePhase.FAIL_CLOSED
                        if generation.delta is CapabilityDeltaKind.RESTRICTION
                        else RuntimePhase.FAILED,
                        code="RUNTIME_STOP_TIMEOUT",
                        message="Supervisor did not stop after drain and forced termination",
                    )
                    raise ConfigError("RUNTIME_STOP_TIMEOUT: supervisor did not stop after drain")
        staged = self._configs.activation_target()
        if staged is None or staged.generation != generation.generation:
            self._configs.stage_activation(
                generation.generation, expected_active=previous.generation if previous else None
            )
        startup_error: Exception | None = None
        try:
            self._launcher.start(self._config_path, foreground=False, extra_env=extra_env)
        except Exception as exc:  # adapter failures are classified by rollback policy below
            startup_error = exc
        if startup_error is None and not wait_for_health:
            return ActivationResult(
                "starting",
                generation.generation,
                previous.generation if previous else None,
                None,
                correlation_id,
                "Activation started; run `rf runtime status` to observe health and final generation.",
            )
        if startup_error is None and self._wait_generation(generation.generation):
            return ActivationResult(
                "active",
                generation.generation,
                generation.generation,
                None,
                correlation_id,
                "The accepted generation is active and healthy.",
            )

        with contextlib.suppress(ConfigError):
            self._configs.clear_activation_target(expected_generation=generation.generation)
        rollback_allowed = (
            rollback_on_failure
            and generation.delta is not CapabilityDeltaKind.RESTRICTION
            and previous is not None
        )
        if rollback_allowed and previous is not None:
            self._request(self._supervisor_control, ControlCommand.SHUTDOWN, correlation_id)
            self._wait_stopped()
            self._configs.stage_activation(previous.generation, expected_active=previous.generation)
            rollback_error: Exception | None = None
            try:
                self._launcher.start(self._config_path, foreground=False, extra_env=extra_env)
            except Exception as exc:
                rollback_error = exc
            if rollback_error is None and self._wait_generation(previous.generation):
                return ActivationResult(
                    "rolled_back",
                    generation.generation,
                    previous.generation,
                    previous.generation,
                    correlation_id,
                    "Review the failed generation before approving it again.",
                )
            with contextlib.suppress(ConfigError):
                self._configs.clear_activation_target(expected_generation=previous.generation)
            details = [
                redact_text(f"{type(item).__name__}: {item}")
                for item in (startup_error, rollback_error)
                if item is not None
            ]
            message = "Activation and rollback failed; runtime remains unavailable"
            if details:
                message += ": " + "; ".join(details)
            self._mark_terminal_failure(
                running,
                generation,
                correlation_id,
                phase=RuntimePhase.FAILED,
                code="ACTIVATION_AND_ROLLBACK_FAILED",
                message=message,
            )
            raise ConfigError(f"ACTIVATION_AND_ROLLBACK_FAILED: {message}") from (
                rollback_error or startup_error
            )
        detail = (
            f": {redact_text(f'{type(startup_error).__name__}: {startup_error}')}"
            if startup_error
            else ""
        )
        message = f"Old capability was not restored; runtime remains fail-closed{detail}"
        self._mark_terminal_failure(
            running,
            generation,
            correlation_id,
            phase=RuntimePhase.FAIL_CLOSED,
            code="RESTRICTIVE_ACTIVATION_FAILED",
            message=message,
        )
        raise ConfigError(f"RESTRICTIVE_ACTIVATION_FAILED: {message}") from startup_error
