"""Typed transition helpers for the ProcessLease state machine.

Each helper validates against ``_ALLOWED_TRANSITIONS`` and returns a new frozen
instance; there is no generic ``transition(status)`` setter, so an illegal move
fails at the call site with a typed error.
"""

from __future__ import annotations

from dataclasses import replace

from .process_lease import ProcessLease, ProcessLeaseStatus, _validate_transition


def _transition(
    lease: ProcessLease,
    target: ProcessLeaseStatus,
    *,
    updated_at: str,
    pid: int | None = None,
    pgid: int | None = None,
    process_identity: str | None = None,
    process_start_token: str | None = None,
    owner_pid: int | None = None,
    owner_process_identity: str | None = None,
    release_sha: str | None = None,
    generation: int | None = None,
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
        pgid=pgid if pgid is not None else lease.pgid,
        process_identity=(
            process_identity if process_identity is not None else lease.process_identity
        ),
        process_start_token=(
            process_start_token if process_start_token is not None else lease.process_start_token
        ),
        owner_pid=owner_pid if owner_pid is not None else lease.owner_pid,
        owner_process_identity=(
            owner_process_identity
            if owner_process_identity is not None
            else lease.owner_process_identity
        ),
        release_sha=release_sha if release_sha is not None else lease.release_sha,
        generation=generation if generation is not None else lease.generation,
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
    pgid: int | None = None,
    process_start_token: str | None = None,
) -> ProcessLease:
    """REGISTERED -> READY with the verified process identity and PID."""
    return _transition(
        lease,
        ProcessLeaseStatus.READY,
        updated_at=updated_at,
        process_identity=process_identity,
        pid=pid,
        pgid=pgid,
        process_start_token=process_start_token,
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
    """READY/RUNNING -> UNPROVEN when identity cannot be proven."""
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
