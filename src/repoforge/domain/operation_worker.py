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
from .operation_identity import OperationIdentityReference
from .operation_task import validate_operation_id

OPERATION_WORKER_BINDING_SCHEMA_VERSION = 3

_MAX_START_TOKEN = 128


@dataclass(frozen=True, slots=True)
class OperationWorkerBinding:
    """Identity of the OS process group running one background operation.

    ``owner_generation`` (schema v2) records which runtime generation spawned the
    work, so a generation handoff can tell an old draining generation's bindings
    apart from the new generation's and reconcile them (#270). It is optional and
    defaults to ``None`` for pre-v2 bindings, which are attributed by the
    ``server_pid``/``server_start_token`` process identity instead.
    """

    operation_id: str
    child_pid: int
    child_pgid: int
    child_start_token: str | None
    server_pid: int
    server_start_token: str | None
    created_at: str
    owner_generation: int | None = None
    owner_id: str | None = None
    attempt: int | None = None
    identity_context_id: str | None = None
    identity_context_digest: str | None = None


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
    if binding.owner_generation is not None:
        _positive_pid(binding.owner_generation, "owner_generation")
    if binding.owner_id is not None and (
        not isinstance(binding.owner_id, str)
        or not binding.owner_id
        or len(binding.owner_id) > 128
        or any(ord(character) < 32 for character in binding.owner_id)
    ):
        raise _error("owner_id is invalid or exceeds 128 characters")
    if binding.attempt is not None:
        _positive_pid(binding.attempt, "attempt")
    if (binding.identity_context_id is None) != (binding.identity_context_digest is None):
        raise _error("identity context id and digest must be set or cleared together")
    if binding.identity_context_id is not None and binding.identity_context_digest is not None:
        try:
            OperationIdentityReference(
                binding.identity_context_id,
                binding.identity_context_digest,
            )
        except ValueError as exc:
            raise _error("worker binding identity context reference is invalid") from exc
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
        "owner_generation": binding.owner_generation,
        "owner_id": binding.owner_id,
        "attempt": binding.attempt,
        "identity_context_id": binding.identity_context_id,
        "identity_context_digest": binding.identity_context_digest,
    }


def _as_int(value: object, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise _error(f"{field} must be an integer")
    return value


def worker_binding_from_payload(payload: dict[str, object]) -> OperationWorkerBinding:
    required = {
        "operation_id",
        "child_pid",
        "child_pgid",
        "child_start_token",
        "server_pid",
        "server_start_token",
        "created_at",
    }
    # Additive optional fields are absent in older records and decode as None:
    # owner_generation/owner_id/attempt from durable execution, and the
    # identity-context reference from operation-scoped identities.
    allowed = required | {
        "owner_generation",
        "owner_id",
        "attempt",
        "identity_context_id",
        "identity_context_digest",
    }
    if not required.issubset(payload) or (set(payload) - allowed):
        raise _error("worker binding payload fields do not match the schema")
    child_start = payload["child_start_token"]
    server_start = payload["server_start_token"]
    owner_generation_raw = payload.get("owner_generation")
    owner_id_raw = payload.get("owner_id")
    attempt_raw = payload.get("attempt")
    owner_generation = (
        None if owner_generation_raw is None else _as_int(owner_generation_raw, "owner_generation")
    )
    binding = OperationWorkerBinding(
        operation_id=str(payload["operation_id"]),
        child_pid=_as_int(payload["child_pid"], "child_pid"),
        child_pgid=_as_int(payload["child_pgid"], "child_pgid"),
        child_start_token=None if child_start is None else str(child_start),
        server_pid=_as_int(payload["server_pid"], "server_pid"),
        server_start_token=None if server_start is None else str(server_start),
        created_at=str(payload["created_at"]),
        owner_generation=owner_generation,
        owner_id=None if owner_id_raw is None else str(owner_id_raw),
        attempt=None if attempt_raw is None else _as_int(attempt_raw, "attempt"),
        identity_context_id=(
            None
            if payload.get("identity_context_id") is None
            else str(payload["identity_context_id"])
        ),
        identity_context_digest=(
            None
            if payload.get("identity_context_digest") is None
            else str(payload["identity_context_digest"])
        ),
    )
    return validate_operation_worker_binding(binding)
