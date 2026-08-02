"""Durable process lease — typed state machine for live OS processes."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from typing import NewType

from typing_extensions import assert_never

LeaseId = NewType("LeaseId", str)


class ProcessLeaseRole(str, Enum):
    """Which subsystem a lease's process belongs to.

    One registry serves every managed process kind so completeness, pagination,
    quarantine, and reconciliation use a common substrate (F-008): the execution
    daemon, short-lived operation workers, and the serve-child of a generation all
    live in the same lease table distinguished by role.
    """

    EXECUTION_DAEMON = "execution_daemon"
    OPERATION_WORKER = "operation_worker"
    TUNNEL_CHILD = "tunnel_child"


def _validate_role(role: ProcessLeaseRole) -> ProcessLeaseRole:
    match role:
        case (
            ProcessLeaseRole.EXECUTION_DAEMON
            | ProcessLeaseRole.OPERATION_WORKER
            | ProcessLeaseRole.TUNNEL_CHILD
        ):
            return role
        case _ as unreachable:
            assert_never(unreachable)


class ProcessLeaseStatus(str, Enum):
    REGISTERED = "registered"
    READY = "ready"
    UNPROVEN = "unproven"
    RUNNING = "running"
    TERMINATING = "terminating"
    TERMINATED = "terminated"
    ARCHIVED = "archived"
    KILLED = "killed"
    QUARANTINED = "quarantined"


_ALLOWED_TRANSITIONS: dict[ProcessLeaseStatus, frozenset[ProcessLeaseStatus]] = {
    ProcessLeaseStatus.REGISTERED: frozenset(
        {ProcessLeaseStatus.READY, ProcessLeaseStatus.TERMINATED}
    ),
    ProcessLeaseStatus.READY: frozenset({ProcessLeaseStatus.UNPROVEN, ProcessLeaseStatus.RUNNING}),
    ProcessLeaseStatus.UNPROVEN: frozenset(
        {ProcessLeaseStatus.RUNNING, ProcessLeaseStatus.TERMINATED}
    ),
    ProcessLeaseStatus.RUNNING: frozenset(
        {
            ProcessLeaseStatus.TERMINATING,
            ProcessLeaseStatus.KILLED,
            ProcessLeaseStatus.QUARANTINED,
        }
    ),
    ProcessLeaseStatus.TERMINATING: frozenset(
        {ProcessLeaseStatus.TERMINATED, ProcessLeaseStatus.KILLED}
    ),
    ProcessLeaseStatus.TERMINATED: frozenset({ProcessLeaseStatus.ARCHIVED}),
    ProcessLeaseStatus.KILLED: frozenset({ProcessLeaseStatus.TERMINATED}),
    ProcessLeaseStatus.QUARANTINED: frozenset({ProcessLeaseStatus.TERMINATED}),
    ProcessLeaseStatus.ARCHIVED: frozenset(),
}


def _require_non_empty(value: str, field: str) -> str:
    if not value or not value.strip():
        raise ValueError(f"Process lease {field} must be a non-empty string")
    return value


def _validate_status(status: ProcessLeaseStatus) -> ProcessLeaseStatus:
    match status:
        case (
            ProcessLeaseStatus.REGISTERED
            | ProcessLeaseStatus.READY
            | ProcessLeaseStatus.UNPROVEN
            | ProcessLeaseStatus.RUNNING
            | ProcessLeaseStatus.TERMINATING
            | ProcessLeaseStatus.TERMINATED
            | ProcessLeaseStatus.ARCHIVED
            | ProcessLeaseStatus.KILLED
            | ProcessLeaseStatus.QUARANTINED
        ):
            return status
        case _ as unreachable:
            assert_never(unreachable)


def _validate_transition(current: ProcessLeaseStatus, target: ProcessLeaseStatus) -> None:
    _validate_status(current)
    _validate_status(target)
    allowed = _ALLOWED_TRANSITIONS[current]
    if target not in allowed:
        raise ValueError(f"Invalid process lease transition: {current.value} -> {target.value}")


@dataclass(frozen=True, slots=True)
class ProcessLease:
    lease_id: str
    status: ProcessLeaseStatus
    process_identity: str | None
    pid: int | None
    started_at: str | None
    heartbeat_at: str | None
    correlation_id: str
    created_at: str
    updated_at: str
    error_code: str | None = None
    error_message: str | None = None
    role: ProcessLeaseRole = ProcessLeaseRole.EXECUTION_DAEMON

    def __post_init__(self) -> None:
        _require_non_empty(self.lease_id, "lease_id")
        _require_non_empty(self.correlation_id, "correlation_id")
        _require_non_empty(self.created_at, "created_at")
        _require_non_empty(self.updated_at, "updated_at")
        _validate_status(self.status)
        _validate_role(self.role)
        if self.pid is not None and self.pid <= 0:
            raise ValueError("Process lease pid must be a positive integer")
        for field, value in (
            ("started_at", self.started_at),
            ("heartbeat_at", self.heartbeat_at),
        ):
            if value is not None and not value.strip():
                raise ValueError(f"Process lease {field} must be a non-empty string")
        for field, value in (
            ("process_identity", self.process_identity),
            ("error_code", self.error_code),
            ("error_message", self.error_message),
        ):
            if value is not None and not value.strip():
                raise ValueError(f"Process lease {field} must be a non-empty string")

    @property
    def lease_id_typed(self) -> LeaseId:
        return LeaseId(self.lease_id)


def process_lease_payload(lease: ProcessLease) -> dict[str, object]:
    """Serialize a lease for durable storage; enums become their string values."""
    return {
        "lease_id": lease.lease_id,
        "status": lease.status.value,
        "role": lease.role.value,
        "process_identity": lease.process_identity,
        "pid": lease.pid,
        "started_at": lease.started_at,
        "heartbeat_at": lease.heartbeat_at,
        "correlation_id": lease.correlation_id,
        "created_at": lease.created_at,
        "updated_at": lease.updated_at,
        "error_code": lease.error_code,
        "error_message": lease.error_message,
    }


def process_lease_from_payload(payload: dict[str, object]) -> ProcessLease:
    """Decode a stored payload; unknown status or role raises ValueError."""
    required = {
        "lease_id",
        "status",
        "role",
        "correlation_id",
        "created_at",
        "updated_at",
    }
    if not required.issubset(payload):
        raise ValueError("process lease payload is missing required fields")
    return ProcessLease(
        lease_id=str(payload["lease_id"]),
        status=ProcessLeaseStatus(str(payload["status"])),
        role=ProcessLeaseRole(str(payload["role"])),
        process_identity=_optional_str(payload.get("process_identity")),
        pid=_optional_int(payload.get("pid")),
        started_at=_optional_str(payload.get("started_at")),
        heartbeat_at=_optional_str(payload.get("heartbeat_at")),
        correlation_id=str(payload["correlation_id"]),
        created_at=str(payload["created_at"]),
        updated_at=str(payload["updated_at"]),
        error_code=_optional_str(payload.get("error_code")),
        error_message=_optional_str(payload.get("error_message")),
    )


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    return str(value)


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("process lease pid must be an integer")
    return value


def _transition(
    lease: ProcessLease,
    target: ProcessLeaseStatus,
    *,
    updated_at: str,
    pid: int | None = None,
    process_identity: str | None = None,
    started_at: str | None = None,
    heartbeat_at: str | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
) -> ProcessLease:
    _validate_transition(lease.status, target)
    return replace(
        lease,
        status=target,
        updated_at=updated_at,
        pid=pid if pid is not None else lease.pid,
        process_identity=(
            process_identity if process_identity is not None else lease.process_identity
        ),
        started_at=started_at if started_at is not None else lease.started_at,
        heartbeat_at=heartbeat_at if heartbeat_at is not None else lease.heartbeat_at,
        error_code=error_code if error_code is not None else lease.error_code,
        error_message=error_message if error_message is not None else lease.error_message,
    )


def register_ready(
    lease: ProcessLease,
    *,
    updated_at: str,
    process_identity: str,
    pid: int,
) -> ProcessLease:
    """REGISTERED -> READY with the verified process identity and PID."""
    return _transition(
        lease,
        ProcessLeaseStatus.READY,
        updated_at=updated_at,
        process_identity=process_identity,
        pid=pid,
    )


def mark_running(
    lease: ProcessLease,
    *,
    updated_at: str,
) -> ProcessLease:
    """READY -> RUNNING once the worker is durably registered and live."""
    return _transition(
        lease,
        ProcessLeaseStatus.RUNNING,
        updated_at=updated_at,
    )


def abort_intent(
    lease: ProcessLease,
    *,
    updated_at: str,
    error_code: str,
    error_message: str,
) -> ProcessLease:
    """REGISTERED -> TERMINATED when the pre-spawn intent is abandoned.

    A pre-spawn lease can be aborted before any process exists (identity could not
    be proven, the durable write failed, the spawn was refused). It is terminal so
    the intent can never dangle as a permanent anomaly.
    """
    return _transition(
        lease,
        ProcessLeaseStatus.TERMINATED,
        updated_at=updated_at,
        error_code=error_code,
        error_message=error_message,
    )


def mark_unproven(
    lease: ProcessLease,
    *,
    updated_at: str,
    error_code: str,
    error_message: str,
) -> ProcessLease:
    """READY -> UNPROVEN when identity cannot be proven."""
    return _transition(
        lease,
        ProcessLeaseStatus.UNPROVEN,
        updated_at=updated_at,
        error_code=error_code,
        error_message=error_message,
    )


def begin_termination(
    lease: ProcessLease,
    *,
    updated_at: str,
    heartbeat_at: str | None = None,
) -> ProcessLease:
    """RUNNING -> TERMINATING (or UNPROVEN -> TERMINATED via confirm)."""
    return _transition(
        lease,
        ProcessLeaseStatus.TERMINATING,
        updated_at=updated_at,
        heartbeat_at=heartbeat_at,
    )


def confirm_terminated(
    lease: ProcessLease,
    *,
    updated_at: str,
) -> ProcessLease:
    """UNPROVEN/TERMINATING/KILLED/QUARANTINED -> TERMINATED."""
    return _transition(lease, ProcessLeaseStatus.TERMINATED, updated_at=updated_at)


def archive(
    lease: ProcessLease,
    *,
    updated_at: str,
) -> ProcessLease:
    """TERMINATED -> ARCHIVED (terminal)."""
    return _transition(lease, ProcessLeaseStatus.ARCHIVED, updated_at=updated_at)


def survive_kill(
    lease: ProcessLease,
    *,
    updated_at: str,
) -> ProcessLease:
    """RUNNING/TERMINATING -> KILLED when SIGKILL did not terminate the process."""
    return _transition(lease, ProcessLeaseStatus.KILLED, updated_at=updated_at)


def quarantine(
    lease: ProcessLease,
    *,
    updated_at: str,
    reason_code: str,
    reason_message: str,
) -> ProcessLease:
    """RUNNING -> QUARANTINED when the operator removes the record."""
    return _transition(
        lease,
        ProcessLeaseStatus.QUARANTINED,
        updated_at=updated_at,
        error_code=reason_code,
        error_message=reason_message,
    )


__all__ = [
    "LeaseId",
    "ProcessLease",
    "ProcessLeaseRole",
    "ProcessLeaseStatus",
    "abort_intent",
    "archive",
    "begin_termination",
    "confirm_terminated",
    "mark_running",
    "mark_unproven",
    "process_lease_from_payload",
    "process_lease_payload",
    "quarantine",
    "register_ready",
    "survive_kill",
]
