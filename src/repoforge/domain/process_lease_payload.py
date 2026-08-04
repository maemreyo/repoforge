"""Payload codec for the durable ProcessLease record."""

from __future__ import annotations

from .process_lease import ProcessLease, ProcessLeaseRole, ProcessLeaseStatus


def process_lease_payload(lease: ProcessLease) -> dict[str, object]:
    """Serialize a lease for durable storage; enums become their string values."""
    return {
        "lease_id": lease.lease_id,
        "status": lease.status.value,
        "role": lease.role.value,
        "process_identity": lease.process_identity,
        "pid": lease.pid,
        "pgid": lease.pgid,
        "process_start_token": lease.process_start_token,
        "owner_pid": lease.owner_pid,
        "owner_process_identity": lease.owner_process_identity,
        "release_sha": lease.release_sha,
        "generation": lease.generation,
        "admission_epoch": lease.admission_epoch,
        "started_at": lease.started_at,
        "heartbeat_at": lease.heartbeat_at,
        "correlation_id": lease.correlation_id,
        "created_at": lease.created_at,
        "updated_at": lease.updated_at,
        "error_code": lease.error_code,
        "error_message": lease.error_message,
    }


def process_lease_from_payload(payload: dict[str, object]) -> ProcessLease:
    """Decode a stored payload; unknown status or role raises ValueError.

    Fields added after the first schema version are optional: a payload written by
    an older release decodes with those fields ``None``, so the registry stays
    readable across upgrades.
    """
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
        pgid=_optional_int(payload.get("pgid")),
        process_start_token=_optional_str(payload.get("process_start_token")),
        owner_pid=_optional_int(payload.get("owner_pid")),
        owner_process_identity=_optional_str(payload.get("owner_process_identity")),
        release_sha=_optional_str(payload.get("release_sha")),
        generation=_optional_int(payload.get("generation")),
        admission_epoch=_optional_int(payload.get("admission_epoch")),
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
