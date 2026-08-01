"""Durable identity of one isolated execution worker process (#368).

An execution worker is spawned by a supervisor with ``start_new_session=True`` (so its
process group id equals its pid) and is tracked only in the supervisor's RAM. If the
supervisor dies, the next supervisor has no record the worker existed. This binding is
that durable record: enough identity to prove -- later, from another process -- that a
given pid is still the same execution worker, and to reap its process group without
touching unrelated processes from the same release.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

EXECUTION_WORKER_BINDING_SCHEMA_VERSION = 1
EXECUTION_WORKER_ARCHIVE_SCHEMA_VERSION = 1

_EXECUTION_WORKER_MODULE = "repoforge.interfaces.runtime.execution_worker"
_WORKER_ID = re.compile(r"^worker-[0-9a-f]+(?:-[0-9a-f]{8,64})?$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_RELEASE_SHA = re.compile(r"^[0-9a-f]{7,40}$")

_KNOWN_STATES = frozenset(
    {
        "running",
        "legacy_unproven",
        "reclaimed",
        "already_gone",
        "survived_kill",
        "refused_unproven",
    }
)

#: Terminal states are history: a lease in one of these is archived and removed
#: from the active registry so the bounded scan can never overflow (#424).
TERMINAL_STATES = frozenset({"reclaimed", "already_gone"})


@dataclass(frozen=True, slots=True)
class ExecutionWorkerBinding:
    """Identity of one execution worker and of the supervisor that owns it."""

    worker_id: str
    pid: int
    pgid: int
    process_start_token: str | None
    generation: int
    release_sha: str | None
    supervisor_pid: int
    supervisor_process_identity: str
    correlation_id: str
    started_at: str
    state: str

    @property
    def child_pid(self) -> int:
        """Alias for the reaper's structural process-group contract (#368)."""
        return self.pid

    @property
    def child_pgid(self) -> int:
        return self.pgid

    @property
    def child_start_token(self) -> str | None:
        return self.process_start_token


def _require(value: object, name: str) -> object:
    if not isinstance(value, str) or not value:
        raise ValueError(f"execution worker {name} must be a non-empty string")
    return value


def _positive_pid(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"execution worker {name} must be a positive integer")
    return value


def _optional_sha(value: object, name: str, pattern: re.Pattern[str]) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ValueError(f"execution worker {name} must be a valid digest")
    return value


def _start_token(value: object) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 128
        or any(ord(character) < 32 for character in value)
    ):
        raise ValueError("execution worker process_start_token is invalid")
    return value


def _text(value: object, name: str, *, limit: int) -> str:
    if not isinstance(value, str) or not value or len(value) > limit:
        raise ValueError(f"execution worker {name} is invalid")
    return value


def validate_execution_worker_binding(
    binding: ExecutionWorkerBinding,
) -> ExecutionWorkerBinding:
    _require(binding.worker_id, "worker_id")
    if not _WORKER_ID.fullmatch(binding.worker_id):
        raise ValueError("execution worker worker_id must look like worker-<hex>")
    _positive_pid(binding.pid, "pid")
    _positive_pid(binding.pgid, "pgid")
    if binding.pgid != binding.pid:
        raise ValueError("execution worker pgid must equal its pid (start_new_session)")
    _start_token(binding.process_start_token)
    _positive_pid(binding.generation, "generation")
    _optional_sha(binding.release_sha, "release_sha", _RELEASE_SHA)
    _positive_pid(binding.supervisor_pid, "supervisor_pid")
    _optional_sha(binding.supervisor_process_identity, "supervisor_process_identity", _SHA256)
    if binding.supervisor_process_identity is None:
        raise ValueError("execution worker supervisor_process_identity must be a sha256 digest")
    _text(binding.correlation_id, "correlation_id", limit=128)
    _text(binding.started_at, "started_at", limit=64)
    if binding.state not in _KNOWN_STATES:
        raise ValueError(f"execution worker state must be one of {sorted(_KNOWN_STATES)}")
    if binding.state == "running" and binding.process_start_token is None:
        # Without a start token, PID-reuse safety can never be proven (#420).
        raise ValueError("execution worker running binding requires a process_start_token")
    return binding


def execution_worker_binding_payload(binding: ExecutionWorkerBinding) -> dict[str, object]:
    validate_execution_worker_binding(binding)
    return {
        "worker_id": binding.worker_id,
        "pid": binding.pid,
        "pgid": binding.pgid,
        "process_start_token": binding.process_start_token,
        "generation": binding.generation,
        "release_sha": binding.release_sha,
        "supervisor_pid": binding.supervisor_pid,
        "supervisor_process_identity": binding.supervisor_process_identity,
        "correlation_id": binding.correlation_id,
        "started_at": binding.started_at,
        "state": binding.state,
    }


def execution_worker_binding_from_payload(
    payload: dict[str, object],
) -> ExecutionWorkerBinding:
    required = {
        "worker_id",
        "pid",
        "pgid",
        "generation",
        "supervisor_pid",
        "supervisor_process_identity",
        "correlation_id",
        "started_at",
        "state",
    }
    optional = {"release_sha", "process_start_token"}
    allowed = required | optional
    if not required.issubset(payload) or (set(payload) - allowed):
        raise ValueError("execution worker binding payload fields do not match the schema")

    def as_int(value: object, name: str) -> int:
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError(f"execution worker {name} must be an integer")
        return value

    # Optional fields are absent in records written before they existed; absence reads
    # as None (release_sha/process_start_token), never as an unreadable record.
    state = str(_require(payload["state"], "state"))
    if state == "running" and payload.get("process_start_token") is None:
        # Records written before the token was mandatory (#420) are read back as an
        # unproven live concern, never as a valid modern `running` binding.
        state = "legacy_unproven"
    binding = ExecutionWorkerBinding(
        worker_id=str(_require(payload["worker_id"], "worker_id")),
        pid=as_int(payload["pid"], "pid"),
        pgid=as_int(payload["pgid"], "pgid"),
        process_start_token=_start_token(payload.get("process_start_token")),
        generation=as_int(payload["generation"], "generation"),
        release_sha=_optional_sha(payload.get("release_sha"), "release_sha", _RELEASE_SHA),
        supervisor_pid=as_int(payload["supervisor_pid"], "supervisor_pid"),
        supervisor_process_identity=str(
            _require(payload["supervisor_process_identity"], "supervisor_process_identity")
        ),
        correlation_id=str(_require(payload["correlation_id"], "correlation_id")),
        started_at=str(_require(payload["started_at"], "started_at")),
        state=state,
    )
    return validate_execution_worker_binding(binding)


def is_execution_worker_entry_point(argv: tuple[str, ...]) -> bool:
    """Is this command line exactly the execution-worker entry point?

    The worker is spawned as ``python -m repoforge.interfaces.runtime.execution_worker
    --config ... --generation N``. Only the exact module in the ``-m`` slot qualifies;
    a module prefix (``repoforge.interfaces.runtime``) must never match, and nothing
    else (``-c`` scripts, the supervisor, MCP serve) is ever an auto-reclaim candidate.
    """
    return len(argv) >= 3 and argv[1] == "-m" and argv[2] == _EXECUTION_WORKER_MODULE


@dataclass(frozen=True, slots=True)
class ExecutionWorkerArchiveEntry:
    """Immutable record of one terminated worker lease (#424).

    Written when an active lease reaches a terminal state, then the lease itself is
    removed from the active registry. Keeps the active registry bounded by the number
    of concurrent workers instead of the number of workers that ever existed.
    """

    worker_id: str
    pid: int
    pgid: int
    process_start_token: str | None
    generation: int
    release_sha: str | None
    supervisor_pid: int
    supervisor_process_identity: str
    correlation_id: str
    started_at: str
    terminated_at: str
    state: str

    @classmethod
    def from_binding(
        cls, binding: ExecutionWorkerBinding, *, terminated_at: str
    ) -> ExecutionWorkerArchiveEntry:
        return cls(
            worker_id=binding.worker_id,
            pid=binding.pid,
            pgid=binding.pgid,
            process_start_token=binding.process_start_token,
            generation=binding.generation,
            release_sha=binding.release_sha,
            supervisor_pid=binding.supervisor_pid,
            supervisor_process_identity=binding.supervisor_process_identity,
            correlation_id=binding.correlation_id,
            started_at=binding.started_at,
            terminated_at=terminated_at,
            state=binding.state,
        )


def validate_execution_worker_archive_entry(
    entry: ExecutionWorkerArchiveEntry,
) -> ExecutionWorkerArchiveEntry:
    if entry.worker_id != _require(entry.worker_id, "worker_id"):
        raise ValueError("execution worker worker_id must be a non-empty string")
    if not _WORKER_ID.fullmatch(entry.worker_id):
        raise ValueError("execution worker worker_id must look like worker-<hex>")
    _positive_pid(entry.pid, "pid")
    _positive_pid(entry.pgid, "pgid")
    if entry.pgid != entry.pid:
        raise ValueError("execution worker pgid must equal its pid (start_new_session)")
    _start_token(entry.process_start_token)
    _positive_pid(entry.generation, "generation")
    _optional_sha(entry.release_sha, "release_sha", _RELEASE_SHA)
    _positive_pid(entry.supervisor_pid, "supervisor_pid")
    if not _SHA256.fullmatch(entry.supervisor_process_identity):
        raise ValueError("execution worker supervisor_process_identity must be a sha256 digest")
    _text(entry.correlation_id, "correlation_id", limit=128)
    _text(entry.started_at, "started_at", limit=64)
    _text(entry.terminated_at, "terminated_at", limit=64)
    if entry.state not in TERMINAL_STATES:
        raise ValueError(f"execution worker archive state must be one of {sorted(TERMINAL_STATES)}")
    return entry


def execution_worker_archive_payload(entry: ExecutionWorkerArchiveEntry) -> dict[str, object]:
    validate_execution_worker_archive_entry(entry)
    return {
        "worker_id": entry.worker_id,
        "pid": entry.pid,
        "pgid": entry.pgid,
        "process_start_token": entry.process_start_token,
        "generation": entry.generation,
        "release_sha": entry.release_sha,
        "supervisor_pid": entry.supervisor_pid,
        "supervisor_process_identity": entry.supervisor_process_identity,
        "correlation_id": entry.correlation_id,
        "started_at": entry.started_at,
        "terminated_at": entry.terminated_at,
        "state": entry.state,
    }


def execution_worker_archive_from_payload(
    payload: dict[str, object],
) -> ExecutionWorkerArchiveEntry:
    required = {
        "worker_id",
        "pid",
        "pgid",
        "generation",
        "supervisor_pid",
        "supervisor_process_identity",
        "correlation_id",
        "started_at",
        "terminated_at",
        "state",
    }
    optional = {"release_sha", "process_start_token"}
    allowed = required | optional
    if not required.issubset(payload) or (set(payload) - allowed):
        raise ValueError("execution worker archive payload fields do not match the schema")

    def as_int(value: object, name: str) -> int:
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError(f"execution worker {name} must be an integer")
        return value

    entry = ExecutionWorkerArchiveEntry(
        worker_id=str(_require(payload["worker_id"], "worker_id")),
        pid=as_int(payload["pid"], "pid"),
        pgid=as_int(payload["pgid"], "pgid"),
        process_start_token=_start_token(payload.get("process_start_token")),
        generation=as_int(payload["generation"], "generation"),
        release_sha=_optional_sha(payload.get("release_sha"), "release_sha", _RELEASE_SHA),
        supervisor_pid=as_int(payload["supervisor_pid"], "supervisor_pid"),
        supervisor_process_identity=str(
            _require(payload["supervisor_process_identity"], "supervisor_process_identity")
        ),
        correlation_id=str(_require(payload["correlation_id"], "correlation_id")),
        started_at=str(_require(payload["started_at"], "started_at")),
        terminated_at=str(_require(payload["terminated_at"], "terminated_at")),
        state=str(_require(payload["state"], "state")),
    )
    return validate_execution_worker_archive_entry(entry)
