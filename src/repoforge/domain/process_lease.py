"""Durable process lease — typed state machine for live OS processes."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from typing import NewType

from typing_extensions import assert_never

LeaseId = NewType("LeaseId", str)


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
    ProcessLeaseStatus.REGISTERED: frozenset({ProcessLeaseStatus.READY}),
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

    def __post_init__(self) -> None:
        _require_non_empty(self.lease_id, "lease_id")
        _require_non_empty(self.correlation_id, "correlation_id")
        _require_non_empty(self.created_at, "created_at")
        _require_non_empty(self.updated_at, "updated_at")
        _validate_status(self.status)
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
    "ProcessLeaseStatus",
    "archive",
    "begin_termination",
    "confirm_terminated",
    "mark_unproven",
    "quarantine",
    "register_ready",
    "survive_kill",
]
