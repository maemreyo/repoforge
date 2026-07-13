"""Protected MCP runtime host with atomic generation routing."""

from __future__ import annotations

from collections.abc import Callable

from ...application.runtime.hot_reload import AtomicServiceRouter, HotReloadCoordinator
from ...domain.errors import ConfigError
from ...domain.redaction import redact_text
from ...domain.runtime import (
    RUNTIME_CONTROL_PROTOCOL_VERSION,
    ControlCommand,
    ControlRequest,
    ControlResponse,
)


class McpRuntimeHost:
    """Serve runtime-control requests without exposing arbitrary execution."""

    def __init__(
        self,
        *,
        router: AtomicServiceRouter,
        reloader: HotReloadCoordinator,
        on_activated: Callable[[int], None] | None = None,
    ) -> None:
        self.router = router
        self._reloader = reloader
        self._on_activated = on_activated

    @staticmethod
    def _positive_generation(value: object, field: str) -> int:
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise ConfigError(f"INVALID_RELOAD_REQUEST: {field} must be a positive integer")
        return value

    def _reload(self, request: ControlRequest) -> ControlResponse:
        payload = dict(request.payload)
        if len(payload) != len(request.payload) or set(payload) - {
            "generation",
            "expected_active",
        }:
            return ControlResponse(
                RUNTIME_CONTROL_PROTOCOL_VERSION,
                False,
                request.correlation_id,
                "invalid",
                error_code="INVALID_RELOAD_REQUEST",
                message="Reload payload contains unsupported fields",
            )
        try:
            generation = self._positive_generation(payload.get("generation"), "generation")
            expected_raw = payload.get("expected_active")
            expected = (
                None
                if expected_raw is None or expected_raw == 0
                else self._positive_generation(expected_raw, "expected_active")
            )
            result = self._reloader.reload(
                generation,
                expected_active=expected,
                correlation_id=request.correlation_id,
            )
        except Exception as exc:
            message = redact_text(f"{type(exc).__name__}: {exc}")
            text = str(exc)
            code = (
                text.split(":", 1)[0]
                if text.startswith(
                    (
                        "HOT_RELOAD_",
                        "STALE_ACTIVE_GENERATION",
                        "INVALID_RELOAD_REQUEST",
                    )
                )
                else "HOT_RELOAD_FAILED"
            )
            return ControlResponse(
                RUNTIME_CONTROL_PROTOCOL_VERSION,
                False,
                request.correlation_id,
                "reload_failed",
                error_code=code,
                message=message,
            )
        warning: str | None = None
        if self._on_activated is not None:
            try:
                self._on_activated(result.active_generation)
            except Exception as exc:
                warning = redact_text(f"Runtime state update failed: {type(exc).__name__}: {exc}")
        response_payload: dict[str, object] = {
            "previous_generation": result.previous_generation,
            "active_generation": result.active_generation,
            "retired_generation": result.retired_generation or 0,
            "repository_ids": list(result.repository_ids),
            "router": self.router.snapshot(),
        }
        if warning is not None:
            response_payload["warning"] = warning
        return ControlResponse(
            RUNTIME_CONTROL_PROTOCOL_VERSION,
            True,
            request.correlation_id,
            result.status,
            tuple(sorted(response_payload.items())),
        )

    def handle(self, request: ControlRequest) -> ControlResponse:
        if request.command in {ControlCommand.PING, ControlCommand.STATUS}:
            container = self.router.active_container()
            payload = {
                "generation": container.generation,
                "gate": container.gate.snapshot(),
                "router": self.router.snapshot(),
            }
            return ControlResponse(
                RUNTIME_CONTROL_PROTOCOL_VERSION,
                True,
                request.correlation_id,
                str(container.gate.snapshot()["state"]),
                tuple(sorted(payload.items())),
            )
        if request.command is ControlCommand.HEALTH:
            try:
                with self.router.acquire() as container:
                    repositories = container.service.repo_list().get("repositories", [])
                    gate = container.gate.snapshot()
                    healthy = gate["state"] == "open"
                    payload = {
                        "gate": gate,
                        "repository_count": len(repositories),
                        "generation": container.generation,
                        "router": self.router.snapshot(),
                    }
                return ControlResponse(
                    1,
                    healthy,
                    request.correlation_id,
                    "healthy" if healthy else str(gate["state"]),
                    tuple(sorted(payload.items())),
                    None if healthy else "MCP_GATE_NOT_OPEN",
                )
            except Exception as exc:
                return ControlResponse(
                    1,
                    False,
                    request.correlation_id,
                    "unhealthy",
                    error_code="MCP_SELF_CHECK_FAILED",
                    message=redact_text(f"{type(exc).__name__}: {exc}"),
                )
        if request.command is ControlCommand.RELOAD:
            return self._reload(request)
        container = self.router.active_container()
        if request.command is ControlCommand.DRAIN:
            payload = dict(request.payload)
            timeout_value = payload.get("timeout_seconds", 30.0)
            if (
                len(payload) != len(request.payload)
                or set(payload) - {"timeout_seconds"}
                or isinstance(timeout_value, bool)
                or not isinstance(timeout_value, (str, int, float))
            ):
                return ControlResponse(
                    1,
                    False,
                    request.correlation_id,
                    "invalid",
                    error_code="INVALID_DRAIN_TIMEOUT",
                )
            try:
                timeout = float(timeout_value)
            except (TypeError, ValueError):
                timeout = -1.0
            if not 0.0 <= timeout <= 120.0:
                return ControlResponse(
                    1,
                    False,
                    request.correlation_id,
                    "invalid",
                    error_code="INVALID_DRAIN_TIMEOUT",
                )
            container.gate.begin_drain(
                reason="runtime generation activation",
                correlation_id=request.correlation_id,
            )
            idle = container.gate.wait_for_idle(timeout)
            return ControlResponse(
                RUNTIME_CONTROL_PROTOCOL_VERSION,
                idle,
                request.correlation_id,
                "drained" if idle else "drain_timeout",
                tuple(sorted(container.gate.snapshot().items())),
                None if idle else "DRAIN_TIMEOUT",
            )
        if request.command is ControlCommand.RESUME:
            container.gate.reopen()
            return ControlResponse(
                RUNTIME_CONTROL_PROTOCOL_VERSION, True, request.correlation_id, "open"
            )
        if request.command is ControlCommand.FAIL_CLOSED:
            container.gate.fail_closed(
                reason=str(dict(request.payload).get("reason", "runtime safety transition")),
                correlation_id=request.correlation_id,
            )
            return ControlResponse(
                RUNTIME_CONTROL_PROTOCOL_VERSION, True, request.correlation_id, "fail_closed"
            )
        return ControlResponse(
            RUNTIME_CONTROL_PROTOCOL_VERSION,
            False,
            request.correlation_id,
            "unsupported",
            error_code="UNSUPPORTED_CONTROL_COMMAND",
        )
