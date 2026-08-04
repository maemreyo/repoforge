"""Durable process lease — typed state machine for live OS processes.

One registry (F-008) serves every managed process kind. This module is the core
state machine; role, payload codec, and transition helpers live in sibling
modules so each file stays under the 400-line policy.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import NewType

from .process_lease_role import ProcessLeaseRole, _validate_role

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
            # A reconciler that cannot prove a RUNNING worker is the recorded
            # process must be able to record the honest status (refused_unproven)
            # instead of a false TERMINATED claim. UNPROVEN stays an active
            # concern: the process may still be alive and holding locks.
            ProcessLeaseStatus.UNPROVEN,
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

#: Lease statuses that remain a live safety concern for containment. A REGISTERED
#: intent without a pid is the pre-spawn crash window; READY is a claim in flight;
#: RUNNING/TERMINATING/KILLED/QUARANTINED describe a process that may still hold
#: locks. Only TERMINATED/ARCHIVED are history.
ACTIVE_LEASE_STATUSES: frozenset[ProcessLeaseStatus] = frozenset(
    {
        ProcessLeaseStatus.REGISTERED,
        ProcessLeaseStatus.READY,
        ProcessLeaseStatus.UNPROVEN,
        ProcessLeaseStatus.RUNNING,
        ProcessLeaseStatus.TERMINATING,
        ProcessLeaseStatus.KILLED,
        ProcessLeaseStatus.QUARANTINED,
    }
)


def _require_non_empty(value: str, field: str) -> str:
    if not value or not value.strip():
        raise ValueError(f"Process lease {field} must be a non-empty string")
    return value


def _validate_status(status: ProcessLeaseStatus) -> ProcessLeaseStatus:
    if status not in ACTIVE_LEASE_STATUSES and status not in {
        ProcessLeaseStatus.TERMINATED,
        ProcessLeaseStatus.ARCHIVED,
    }:
        raise ValueError(f"Unknown process lease status: {status!r}")
    return status


def _validate_transition(current: ProcessLeaseStatus, target: ProcessLeaseStatus) -> None:
    _validate_status(current)
    _validate_status(target)
    allowed = _ALLOWED_TRANSITIONS[current]
    if target not in allowed:
        raise ValueError(f"Invalid process lease transition: {current.value} -> {target.value}")


def _validate_positive_int(value: int | None, field: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"Process lease {field} must be a positive integer")
    return value


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
    # Single-authority identity and containment fields (F-008): the lease carries
    # everything a reconciler needs to prove and reap a process, so no safety gate
    # has to read the legacy binding projection. All optional so stored payloads
    # written before these fields existed decode unchanged.
    pgid: int | None = None
    process_start_token: str | None = None
    owner_pid: int | None = None
    owner_process_identity: str | None = None
    release_sha: str | None = None
    generation: int | None = None
    admission_epoch: int | None = None

    def __post_init__(self) -> None:
        _require_non_empty(self.lease_id, "lease_id")
        _require_non_empty(self.correlation_id, "correlation_id")
        _require_non_empty(self.created_at, "created_at")
        _require_non_empty(self.updated_at, "updated_at")
        _validate_status(self.status)
        _validate_role(self.role)
        _validate_positive_int(self.pid, "pid")
        _validate_positive_int(self.pgid, "pgid")
        _validate_positive_int(self.owner_pid, "owner_pid")
        _validate_positive_int(self.generation, "generation")
        _validate_positive_int(self.admission_epoch, "admission_epoch")
        for field, value in (
            ("started_at", self.started_at),
            ("heartbeat_at", self.heartbeat_at),
            ("process_identity", self.process_identity),
            ("error_code", self.error_code),
            ("error_message", self.error_message),
            ("process_start_token", self.process_start_token),
            ("owner_process_identity", self.owner_process_identity),
            ("release_sha", self.release_sha),
        ):
            if value is not None and not value.strip():
                raise ValueError(f"Process lease {field} must be a non-empty string")

    @property
    def lease_id_typed(self) -> LeaseId:
        return LeaseId(self.lease_id)


# Re-export the sibling modules' helpers so `from ...domain.process_lease import ...`
# keeps working for every existing caller (payload codec and transition functions).
from .process_lease_payload import (  # noqa: E402
    process_lease_from_payload,
    process_lease_payload,
)
from .process_lease_transitions import (  # noqa: E402
    abort_intent,
    archive,
    begin_termination,
    confirm_terminated,
    mark_running,
    mark_unproven,
    quarantine,
    register_ready,
    survive_kill,
)

__all__ = [
    "ACTIVE_LEASE_STATUSES",
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
