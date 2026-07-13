"""Pure runtime state machine and local control protocol contracts."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, replace
from enum import Enum

RUNTIME_CONTROL_PROTOCOL_VERSION = 1


class RuntimePhase(str, Enum):
    STOPPED = "stopped"
    STARTING = "starting"
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    DRAINING = "draining"
    RELOADING = "reloading"
    STOPPING = "stopping"
    FAILED = "failed"
    FAIL_CLOSED = "fail_closed"


_ALLOWED_TRANSITIONS: dict[RuntimePhase, frozenset[RuntimePhase]] = {
    RuntimePhase.STOPPED: frozenset({RuntimePhase.STARTING}),
    RuntimePhase.STARTING: frozenset(
        {RuntimePhase.HEALTHY, RuntimePhase.DEGRADED, RuntimePhase.FAILED, RuntimePhase.STOPPING}
    ),
    RuntimePhase.HEALTHY: frozenset(
        {
            RuntimePhase.DEGRADED,
            RuntimePhase.DRAINING,
            RuntimePhase.STOPPING,
            RuntimePhase.FAIL_CLOSED,
        }
    ),
    RuntimePhase.DEGRADED: frozenset(
        {
            RuntimePhase.HEALTHY,
            RuntimePhase.DRAINING,
            RuntimePhase.STOPPING,
            RuntimePhase.FAILED,
            RuntimePhase.FAIL_CLOSED,
        }
    ),
    RuntimePhase.DRAINING: frozenset(
        {
            RuntimePhase.RELOADING,
            RuntimePhase.STOPPING,
            RuntimePhase.HEALTHY,
            RuntimePhase.FAIL_CLOSED,
        }
    ),
    RuntimePhase.RELOADING: frozenset(
        {
            RuntimePhase.STARTING,
            RuntimePhase.HEALTHY,
            RuntimePhase.STOPPING,
            RuntimePhase.FAILED,
            RuntimePhase.FAIL_CLOSED,
        }
    ),
    RuntimePhase.STOPPING: frozenset({RuntimePhase.STOPPED, RuntimePhase.FAILED}),
    RuntimePhase.FAILED: frozenset(
        {RuntimePhase.STARTING, RuntimePhase.STOPPED, RuntimePhase.FAIL_CLOSED}
    ),
    RuntimePhase.FAIL_CLOSED: frozenset({RuntimePhase.STARTING, RuntimePhase.STOPPED}),
}


_SHA256 = re.compile(r"^[a-f0-9]{64}$")


@dataclass(frozen=True, slots=True)
class TunnelProfile:
    tunnel_id_fingerprint: str
    profile: str
    executable: str
    executable_version: str
    mcp_argv: tuple[str, ...]

    def __post_init__(self) -> None:
        if (
            not _SHA256.fullmatch(self.tunnel_id_fingerprint)
            or not self.profile
            or not self.executable
            or not self.executable_version
            or not self.mcp_argv
            or not all(self.mcp_argv)
        ):
            raise ValueError(
                "Tunnel profile requires a hashed id, executable identity and MCP argv"
            )

    @property
    def fingerprint(self) -> str:
        value = json.dumps(
            {
                "tunnel_id_fingerprint": self.tunnel_id_fingerprint,
                "profile": self.profile,
                "executable": self.executable,
                "executable_version": self.executable_version,
                "mcp_argv": self.mcp_argv,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(value.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class ChildProcess:
    pid: int
    process_identity: str
    started_at: str

    def __post_init__(self) -> None:
        if self.pid <= 0 or not _SHA256.fullmatch(self.process_identity) or not self.started_at:
            raise ValueError("Child process identity is invalid")


@dataclass(frozen=True, slots=True)
class RuntimeRecord:
    protocol_version: int
    phase: RuntimePhase
    pid: int | None
    process_identity: str | None
    active_generation: int | None
    accepted_generation: int
    tunnel_profile: str
    tunnel_profile_fingerprint: str
    tool_surface_hash: str
    started_at: str | None
    updated_at: str
    correlation_id: str
    child_pid: int | None = None
    child_process_identity: str | None = None
    restart_count: int = 0
    last_error_code: str | None = None
    last_error: str | None = None
    health: tuple[tuple[str, bool, str], ...] = ()

    def __post_init__(self) -> None:
        if (
            self.protocol_version != RUNTIME_CONTROL_PROTOCOL_VERSION
            or self.accepted_generation <= 0
        ):
            raise ValueError("Runtime record protocol or accepted generation is invalid")
        for pid, identity in (
            (self.pid, self.process_identity),
            (self.child_pid, self.child_process_identity),
        ):
            if pid is None:
                if identity is not None:
                    raise ValueError("Process identity cannot exist without a PID")
            elif pid <= 0 or identity is None or not _SHA256.fullmatch(identity):
                raise ValueError("Runtime process identity is invalid")
        if self.active_generation is not None and self.active_generation <= 0:
            raise ValueError("Active generation must be positive")
        if self.restart_count < 0 or not self.updated_at or not self.correlation_id:
            raise ValueError("Runtime record metadata is invalid")
        if self.phase is RuntimePhase.HEALTHY and (
            self.pid is None or self.child_pid is None or self.active_generation is None
        ):
            raise ValueError("Healthy runtime must own supervisor and child processes")

    @property
    def restart_required(self) -> bool:
        return self.active_generation != self.accepted_generation


def transition(
    record: RuntimeRecord,
    phase: RuntimePhase,
    *,
    updated_at: str,
    correlation_id: str,
) -> RuntimeRecord:
    if phase not in _ALLOWED_TRANSITIONS[record.phase]:
        raise ValueError(f"Invalid runtime transition: {record.phase.value} -> {phase.value}")
    return replace(
        record,
        phase=phase,
        updated_at=updated_at,
        correlation_id=correlation_id,
    )


class ControlCommand(str, Enum):
    PING = "ping"
    STATUS = "status"
    HEALTH = "health"
    RELOAD = "reload"
    DRAIN = "drain"
    RESUME = "resume"
    FAIL_CLOSED = "fail_closed"
    SHUTDOWN = "shutdown"


@dataclass(frozen=True, slots=True)
class ControlRequest:
    protocol_version: int
    command: ControlCommand
    correlation_id: str
    payload: tuple[tuple[str, object], ...] = ()


@dataclass(frozen=True, slots=True)
class ControlResponse:
    protocol_version: int
    ok: bool
    correlation_id: str
    status: str
    payload: tuple[tuple[str, object], ...] = ()
    error_code: str | None = None
    message: str | None = None
