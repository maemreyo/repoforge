"""Durable binding of a running operation to the OS worker executing it.

Persisted in a sidecar collection keyed by ``operation_id`` -- deliberately *not*
a field on :class:`OperationTask`, whose record schema is version-pinned and
still awaits the read-time migration framework (#242). Adding a field there would
make every existing on-disk operation record unreadable.

The binding lets a *later* process -- one that started after the process which
spawned the work has died -- reap a detached child that outlived its operation
record, and lets cancellation reach that child across process boundaries. A
background command's subprocess is started with ``start_new_session=True`` so its
process-group id equals its own pid; that group is what a reaper signals.
"""

from __future__ import annotations

from dataclasses import dataclass

from .errors import ErrorCode, RepoForgeError
from .operation_task import validate_operation_id

OPERATION_WORKER_BINDING_SCHEMA_VERSION = 1

_MAX_START_TOKEN = 128


@dataclass(frozen=True, slots=True)
class OperationWorkerBinding:
    """Identity of the OS process group running one background operation."""

    operation_id: str
    child_pid: int
    child_pgid: int
    child_start_token: str | None
    server_pid: int
    server_start_token: str | None
    created_at: str


def _error(message: str) -> RepoForgeError:
    return RepoForgeError(message, code=ErrorCode.STATE_INVALID)


def _positive_pid(value: int, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise _error(f"{field} must be a positive integer")
    return value


def _start_token(value: str | None, field: str) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or not value
        or len(value) > _MAX_START_TOKEN
        or any(ord(character) < 32 for character in value)
    ):
        raise _error(f"{field} is invalid or exceeds {_MAX_START_TOKEN} characters")
    return value


def validate_operation_worker_binding(binding: OperationWorkerBinding) -> OperationWorkerBinding:
    validate_operation_id(binding.operation_id)
    _positive_pid(binding.child_pid, "child_pid")
    _positive_pid(binding.child_pgid, "child_pgid")
    _start_token(binding.child_start_token, "child_start_token")
    _positive_pid(binding.server_pid, "server_pid")
    _start_token(binding.server_start_token, "server_start_token")
    if (
        not isinstance(binding.created_at, str)
        or not binding.created_at
        or len(binding.created_at) > 64
    ):
        raise _error("created_at must be a non-empty ISO-8601 timestamp")
    return binding


def worker_binding_payload(binding: OperationWorkerBinding) -> dict[str, object]:
    validate_operation_worker_binding(binding)
    return {
        "operation_id": binding.operation_id,
        "child_pid": binding.child_pid,
        "child_pgid": binding.child_pgid,
        "child_start_token": binding.child_start_token,
        "server_pid": binding.server_pid,
        "server_start_token": binding.server_start_token,
        "created_at": binding.created_at,
    }


def _as_int(value: object, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise _error(f"{field} must be an integer")
    return value


def worker_binding_from_payload(payload: dict[str, object]) -> OperationWorkerBinding:
    expected = {
        "operation_id",
        "child_pid",
        "child_pgid",
        "child_start_token",
        "server_pid",
        "server_start_token",
        "created_at",
    }
    if set(payload) != expected:
        raise _error("worker binding payload fields do not match the schema")
    child_start = payload["child_start_token"]
    server_start = payload["server_start_token"]
    binding = OperationWorkerBinding(
        operation_id=str(payload["operation_id"]),
        child_pid=_as_int(payload["child_pid"], "child_pid"),
        child_pgid=_as_int(payload["child_pgid"], "child_pgid"),
        child_start_token=None if child_start is None else str(child_start),
        server_pid=_as_int(payload["server_pid"], "server_pid"),
        server_start_token=None if server_start is None else str(server_start),
        created_at=str(payload["created_at"]),
    )
    return validate_operation_worker_binding(binding)
