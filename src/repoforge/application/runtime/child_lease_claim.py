"""Child-side lease claim — closes the pre-spawn crash window (F-001 P0).

The parent writes a REGISTERED intent before Popen and records the pid the
moment Popen returns. If the parent dies between those two writes, the worker
runs with a pid-less lease and no binding — invisible to every reclaim path.
The worker itself closes that window: it knows its own pid and process
identity, and the supervisor's pid/identity arrive via env, so when the
supervisor is provably dead it claims the intent lease and writes the binding
projection. A later reconciler then proves and reclaims the orphan like any
other worker.

The handshake is fail-closed on every orphan path:

* the supervisor is ``alive`` only when the pid exists AND (when an identity
  was recorded) its current process identity still matches — a reused pid that
  is not the recorded supervisor is treated as dead, never as a live owner;
* a dead supervisor with NO lease record means no authority owns this worker's
  lifecycle — the worker self-terminates instead of running invisible;
* a worker whose own identity cannot be proven records the diagnostic pid and
  self-terminates — an unprovable worker must never run as an un-reclaimable
  orphan;
* the claim advances REGISTERED -> READY first, the durable binding is written
  between the claim and the RUNNING acknowledgement, so a crash in between
  leaves READY-with-pid — a state a later reconciler proves and reclaims from
  the canonical lease alone. Recovery never depends on the projection.

The claim only ever runs when the supervisor is dead, so it can never race the
parent's own registration; the CAS-bound ``claim_intent`` additionally refuses
to clobber a handshake the parent did finish, and an already-READY/RUNNING
lease is acknowledged only when its start token matches this worker's (a
different token is PID reuse of the lease, never this worker).
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Callable
from dataclasses import dataclass

from ...domain.execution_worker import ExecutionWorkerBinding
from ...domain.process_lease import ProcessLeaseStatus
from ...ports.execution_worker_store import ExecutionWorkerBindingStore
from ...ports.process_lease_store import ProcessLeaseStore
from ...ports.worker_registrar import WorkerRegistrar

#: Exit code when the worker finds its lease already terminal: it was superseded
#: by an abort or a reclaim, so it must not start doing work.
EXIT_SUPERSEDED = 3
#: Exit code when the lease names a different process than this worker (pid or
#: start-token mismatch: PID reuse of the lease, never this worker).
EXIT_LEASE_CONFLICT = 4
#: Exit code when the supervisor is provably dead but no lease exists at the
#: registry this worker reads: no authority owns the lifecycle, so the worker
#: must not run as an invisible orphan.
EXIT_ORPHANED = 5
#: Exit code when the worker cannot prove its own identity: the diagnostic pid
#: is recorded so the lease stays discoverable, then the worker self-terminates
#: rather than run un-reclaimable.
EXIT_IDENTITY_UNPROVABLE = 6

#: Process-identity reader for the supervisor: ``current_identity(pid) -> str``.
SupervisorIdentityReader = Callable[[int], str | None]


@dataclass(frozen=True, slots=True)
class ChildLeaseClaim:
    """What the child-side handshake decided for one spawned worker."""

    claimed: bool
    acked: bool
    exit_code: int | None
    detail: str


def claim_child_lease(
    *,
    leases: ProcessLeaseStore,
    registrar: WorkerRegistrar,
    bindings: ExecutionWorkerBindingStore,
    lease_id: str,
    supervisor_pid: int,
    supervisor_process_identity: str,
    generation: int,
    release_sha: str | None,
    identity: str | None,
    start_token: str | None,
    now: str,
    supervisor_identity_reader: SupervisorIdentityReader | None = None,
) -> ChildLeaseClaim:
    """Claim/ack/self-terminate one pre-spawn lease from the worker's own side."""
    if _supervisor_alive(supervisor_pid, supervisor_process_identity, supervisor_identity_reader):
        return ChildLeaseClaim(
            claimed=False,
            acked=True,
            exit_code=None,
            detail="supervisor alive; the parent owns the lease lifecycle",
        )
    envelope = leases.read(lease_id)
    if envelope is None:
        # The parent is provably dead and there is no lease at this registry:
        # "the parent owns the lifecycle" is no longer true, and no reconciler
        # can ever discover this process through a record that does not exist.
        # Running on would create an invisible orphan, so fail closed: exit.
        return ChildLeaseClaim(
            claimed=False,
            acked=False,
            exit_code=EXIT_ORPHANED,
            detail="supervisor dead and no lease found; refusing to run as an "
            "un-discoverable orphan",
        )
    lease = envelope.value
    if lease.status in {
        ProcessLeaseStatus.TERMINATED,
        ProcessLeaseStatus.ARCHIVED,
    }:
        return ChildLeaseClaim(
            claimed=False,
            acked=False,
            exit_code=EXIT_SUPERSEDED,
            detail="lease terminal; the worker was superseded",
        )
    if lease.pid is not None and lease.pid != os.getpid():
        return ChildLeaseClaim(
            claimed=False,
            acked=False,
            exit_code=EXIT_LEASE_CONFLICT,
            detail=f"lease names pid {lease.pid}, not {os.getpid()}",
        )
    if lease.status is ProcessLeaseStatus.RUNNING:
        # A fully registered lease: acknowledge only when it is really this
        # worker. A different start token means the lease was reused by another
        # process -- acknowledging would run a duplicate, so fail closed.
        if (
            lease.process_start_token is not None
            and start_token is not None
            and lease.process_start_token != start_token
        ):
            return ChildLeaseClaim(
                claimed=False,
                acked=False,
                exit_code=EXIT_LEASE_CONFLICT,
                detail="running lease names a different start token; PID reuse",
            )
        return ChildLeaseClaim(
            claimed=False,
            acked=True,
            exit_code=None,
            detail="lease already running; ack",
        )
    if identity is None or start_token is None:
        # The identity could not be proven: record the pid so the orphan is at
        # least discoverable, then self-terminate. An unprovable worker that
        # keeps running cannot be auto-reclaimed safely (no PID-reuse proof and
        # no binding), so it becomes a permanent fail-closed blocker -- the safe
        # resolution is to exit and leave the discoverable diagnostic record.
        with contextlib.suppress(Exception):
            registrar.record_pid(
                lease,
                pid=os.getpid(),
                expected_revision=envelope.revision,
                pgid=os.getpid(),
            )
        return ChildLeaseClaim(
            claimed=False,
            acked=False,
            exit_code=EXIT_IDENTITY_UNPROVABLE,
            detail="pid recorded; identity unprovable, self-terminating",
        )
    # REGISTERED or READY with our pid (or no pid yet): claim -> READY, write the
    # durable binding BETWEEN the claim and the RUNNING acknowledgement, so a
    # crash between claim and binding leaves READY-with-pid (recoverable from the
    # canonical lease) instead of a RUNNING lease with no projection.
    claimed_lease, claimed_revision = registrar.claim_intent(
        lease_id,
        process_identity=identity,
        pid=os.getpid(),
        pgid=os.getpid(),
        process_start_token=start_token,
        owner_pid=supervisor_pid,
        owner_process_identity=supervisor_process_identity,
    )
    bindings.put(
        ExecutionWorkerBinding(
            worker_id=lease_id,
            pid=os.getpid(),
            pgid=os.getpid(),
            process_start_token=start_token,
            generation=generation,
            release_sha=release_sha,
            supervisor_pid=supervisor_pid,
            supervisor_process_identity=supervisor_process_identity,
            correlation_id=claimed_lease.correlation_id,
            started_at=now,
            state="running",
        )
    )
    registrar.complete_claim(claimed_lease, expected_revision=claimed_revision)
    return ChildLeaseClaim(
        claimed=True,
        acked=False,
        exit_code=None,
        detail="claim completed; the orphan is discoverable and reclaimable",
    )


def _supervisor_alive(
    pid: int,
    recorded_identity: str,
    identity_reader: SupervisorIdentityReader | None,
) -> bool:
    """Is the pid still the SAME supervisor this worker was spawned under?

    PID existence alone is not proof: when the supervisor died and its pid was
    reused by an unrelated process, ``os.kill(pid, 0)`` succeeds but the process
    is not the recorded owner. When a supervisor identity was recorded (and a
    reader is wired), the pid is only ``alive`` if its CURRENT identity still
    matches the recorded one -- anything else (reused pid, unreadable identity)
    is treated as dead, because assuming a live owner would leave the worker an
    invisible orphan. Without a recorded identity or reader (legacy spawns) the
    check falls back to PID existence.
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    if identity_reader is None or not recorded_identity:
        return True
    return identity_reader(pid) == recorded_identity


__all__ = [
    "EXIT_IDENTITY_UNPROVABLE",
    "EXIT_LEASE_CONFLICT",
    "EXIT_ORPHANED",
    "EXIT_SUPERSEDED",
    "ChildLeaseClaim",
    "SupervisorIdentityReader",
    "claim_child_lease",
]
