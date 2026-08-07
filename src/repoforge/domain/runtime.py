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
    #: Lifetime tunnel-client gives one MCP transport connection. `None` passes nothing and
    #: leaves the tunnel's own default in force, which is what every existing installation
    #: gets. Raising it makes the recycle rarer; it does not remove it, because the client
    #: shuts itself down on expiry rather than reconnecting the stdio child.
    mcp_connection_max_ttl_seconds: int | None = None

    def __post_init__(self) -> None:
        if (
            not _SHA256.fullmatch(self.tunnel_id_fingerprint)
            or not self.profile
            or not self.executable
            or not self.executable_version
            or not self.mcp_argv
            or not all(self.mcp_argv)
            or (
                self.mcp_connection_max_ttl_seconds is not None
                and self.mcp_connection_max_ttl_seconds <= 0
            )
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
                "mcp_connection_max_ttl_seconds": self.mcp_connection_max_ttl_seconds,
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
class HealthCheck:
    """One bounded, explainable runtime health component observation."""

    name: str
    ok: bool
    detail: str

    def __post_init__(self) -> None:
        if (
            not self.name
            or len(self.name) > 64
            or not re.fullmatch(r"[a-z][a-z0-9_]*", self.name)
            or not self.detail
            or len(self.detail) > 1_000
        ):
            raise ValueError("Runtime health check is invalid")

    def legacy(self) -> tuple[str, bool, str]:
        return (self.name, self.ok, self.detail)


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
    package_version: str | None = None
    executable: str | None = None
    install_origin: str | None = None
    # The release this process was launched from, captured by the launcher BEFORE exec.
    # It must never be re-derived from `executable`: a relocatable venv's interpreter is a
    # symlink to a shared uv-managed Python (so resolving it escapes the release), and the
    # `current` symlink is mutable (so mapping it at read time could attribute a still-live
    # old worker to a newly activated release).
    running_release_sha: str | None = None
    health_observed_at: str | None = None
    consecutive_health_failures: int = 0
    # `restart_count` is a gauge the restart policy consumes: it resets after a stable
    # interval, so 60 seconds after the last restart the record reads `0` and an outage
    # leaves no trace at all. During the 2026-07-28 incident the connector was torn down
    # twice in twelve minutes and the durable record afterwards said `healthy, 0 restarts`,
    # which is what sent diagnosis to the raw tunnel log. These two are evidence, not
    # policy: nothing resets them for the life of the record.
    #: Deliberately NOT constrained to be >= `restart_count`. A record written before these
    #: fields existed decodes with `restarts_total = 0` and whatever `restart_count` it had,
    #: and refusing that record makes the runtime unstartable after an upgrade -- observed.
    #: `0` on an older record is honest: nothing was counting then.
    restarts_total: int = 0
    last_restart_at: str | None = None
    # When a supervisor entered resident fail-closed (#367). Once set it is durable:
    # a supervisor later relaunched by launchd honors it for the same release instead
    # of re-probing and re-spawning, so a deterministic failure is never reset by a
    # fresh lifetime. Cleared when a different release's supervisor writes a new record.
    fail_closed_since: str | None = None
    # A fresh, non-reused identifier generated once per supervisor process lifetime
    # (#448 Slice 4) -- distinct from `correlation_id`, which is ALSO reused per-request
    # in `ControlRequest`/`ControlResponse` and is therefore unsuitable as a lifecycle
    # identifier on its own. `None` only for records written before this field existed.
    incarnation_id: str | None = None
    # Whether `restarts_total`/`last_restart_at` above reflect real accumulated history
    # from the durable restart-history ledger ("durable"), or this process could not
    # find one -- a fresh install, or an upgrade from before the ledger existed
    # ("unknown"). Deliberately distinct from a bare `0`: an operator reading
    # `restarts_total: 0` needs to know whether that means "verified zero restarts" or
    # "history predates this build and was never counted" (#448 Slice 4).
    restart_history_provenance: str = "unknown"

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
        if (
            self.restart_count < 0
            or self.consecutive_health_failures < 0
            or self.restarts_total < 0
            or not self.updated_at
            or not self.correlation_id
        ):
            raise ValueError("Runtime record metadata is invalid")
        if self.fail_closed_since is not None and (
            not self.fail_closed_since or len(self.fail_closed_since) > 64
        ):
            raise ValueError("Runtime fail_closed_since is invalid")
        if self.incarnation_id is not None and not self.incarnation_id:
            raise ValueError("Runtime incarnation_id cannot be empty")
        if self.restart_history_provenance not in ("unknown", "durable"):
            raise ValueError("Runtime restart_history_provenance is invalid")
        if self.running_release_sha is not None and not re.fullmatch(
            r"[0-9a-f]{7,40}", self.running_release_sha
        ):
            raise ValueError("Runtime running_release_sha must be lowercase hex")
        for name, value in (
            ("package_version", self.package_version),
            ("executable", self.executable),
            ("install_origin", self.install_origin),
        ):
            if value is not None and (
                not value or len(value) > 1024 or any(ord(c) < 32 for c in value)
            ):
                raise ValueError(f"Runtime {name} is invalid")
        for name, ok, detail in self.health:
            HealthCheck(name, ok, detail)
        if self.health and not self.health_observed_at:
            object.__setattr__(self, "health_observed_at", self.updated_at)
        if self.phase is RuntimePhase.HEALTHY and (
            self.pid is None or self.child_pid is None or self.active_generation is None
        ):
            raise ValueError("Healthy runtime must own supervisor and child processes")

    @property
    def restart_required(self) -> bool:
        return self.active_generation != self.accepted_generation


@dataclass(frozen=True, slots=True)
class RestartHistoryRecord:
    """A durable restart-history ledger, deliberately separate from `RuntimeRecord`.

    `RuntimeRecord` is a single, mutable, self-healing snapshot of the *currently
    observed* process: `JsonRuntimeStore.read()` clears it entirely whenever the
    recorded `pid` no longer matches a live process with the same identity
    fingerprint -- correct for "is this claim about a running process still true,"
    but by construction that check fires on essentially every real process
    replacement, since a brand-new incarnation is never the same pid as the one
    that just died. `restarts_total`/`last_restart_at` were being carried forward
    by reading that same self-healing record, so they were being discarded as
    collateral damage on the very restarts they exist to count (#448 Slice 4).

    This ledger answers a different question -- "what has happened across every
    incarnation of this runtime" -- and is never subject to pid-liveness checks:
    a dead process's restart history is still true history.
    """

    protocol_version: int
    restarts_total: int
    last_restart_at: str | None
    incarnation_id: str
    updated_at: str
    # The idempotency key of the restart event that produced this record's current
    # `restarts_total`/`last_restart_at`. Replaying the same logical restart (a retry
    # after a partial write failure, e.g.) must not increment a second time --
    # `record_restart()` checks this before incrementing (#448 Slice 4).
    last_event_id: str | None = None
    # Why the most recent restart happened, if known -- diagnostic evidence, not a
    # policy input.
    last_restart_reason: str | None = None
    #: "durable" once this ledger has genuinely tracked at least one restart itself;
    #: "legacy_runtime_record" when it was seeded from a pre-ledger `RuntimeRecord`'s
    #: `restarts_total` because no ledger existed yet at upgrade time -- distinct so an
    #: operator reading a seeded value knows it is carried-forward evidence, not
    #: something this ledger observed directly (#448 Slice 4 migration).
    provenance: str = "durable"

    def __post_init__(self) -> None:
        if (
            self.protocol_version != RUNTIME_CONTROL_PROTOCOL_VERSION
            or self.restarts_total < 0
            or not self.incarnation_id
            or not self.updated_at
            or self.provenance not in ("durable", "legacy_runtime_record")
        ):
            raise ValueError("Restart history record is invalid")


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
