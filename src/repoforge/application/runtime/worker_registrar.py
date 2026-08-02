"""Durable worker registration with a pre-spawn intent lease (F-001).

The execution worker is spawned by a supervisor; if the supervisor dies between
``Popen`` and the durable registration, the worker runs with no record a later
supervisor can discover, archive, or quarantine -- the 2026-08-01 orphan shape.
This registrar closes that window by recording a REGISTERED intent BEFORE any
process exists:

    create_intent (REGISTERED, no pid)  -- before Popen
    record_pid   (pid persisted)        -- the moment Popen returns, so a crash
                                          leaves a discoverable record
    complete_registration (READY -> RUNNING) -- only after identity is proven

The parent returns a worker only after the lease is durably RUNNING. The SQLite
shadow mirrors every authoritative write for parity; a shadow failure never
fails the authoritative registration.
"""

from __future__ import annotations

from dataclasses import replace

from ...domain.durable_state import Revision
from ...domain.errors import ConfigError
from ...domain.process_lease import (
    ProcessLease,
    ProcessLeaseRole,
    ProcessLeaseStatus,
)
from ...domain.process_lease import (
    abort_intent as _abort_intent,
)
from ...domain.process_lease import (
    mark_running as _mark_running,
)
from ...domain.process_lease import (
    register_ready as _register_ready,
)
from ...ports.clock import Clock
from ...ports.ids import IdGenerator
from ...ports.process_lease_store import LeaseShadowStore, ProcessLeaseStore


class WorkerRegistrar:
    """Orchestrate the pre-spawn intent -> running lease lifecycle."""

    def __init__(
        self,
        *,
        leases: ProcessLeaseStore,
        ids: IdGenerator,
        clock: Clock,
        shadow: LeaseShadowStore | None = None,
    ) -> None:
        self._leases = leases
        self._ids = ids
        self._clock = clock
        self._shadow = shadow

    def create_intent(
        self, *, role: ProcessLeaseRole, correlation_id: str
    ) -> tuple[ProcessLease, Revision]:
        """Durably record the REGISTERED intent BEFORE any process is spawned.

        Returns the lease together with the revision this caller now owns; every
        later transition must CAS on that owned revision, never on a fresh store
        read, so a concurrent terminalize cannot be resurrected (F-001).
        """
        now = self._clock.now_iso()
        lease = ProcessLease(
            lease_id=f"worker-{self._ids.new_hex(24)}",
            status=ProcessLeaseStatus.REGISTERED,
            role=role,
            process_identity=None,
            pid=None,
            started_at=None,
            heartbeat_at=None,
            correlation_id=correlation_id,
            created_at=now,
            updated_at=now,
        )
        envelope = self._leases.create(lease)
        self._mirror(lease, envelope.revision)
        return lease, envelope.revision

    def record_pid(
        self,
        lease: ProcessLease,
        *,
        pid: int,
        expected_revision: Revision,
    ) -> tuple[ProcessLease, Revision]:
        """Persist the pid the instant Popen returns.

        This is the write that makes a post-crash process discoverable: the lease
        exists before the process, and the pid lands in it before any further work,
        so no crash point leaves a live process with neither a lease nor a pid.
        CAS uses the caller's owned revision; a stale writer fails instead of
        resurrecting a terminalized lease.
        """
        updated = replace(lease, pid=pid, updated_at=self._clock.now_iso())
        envelope = self._leases.save(updated, expected_revision=expected_revision)
        self._mirror(updated, envelope.revision)
        return updated, envelope.revision

    def complete_registration(
        self,
        lease: ProcessLease,
        *,
        process_identity: str,
        expected_revision: Revision,
    ) -> tuple[ProcessLease, Revision]:
        """READY -> RUNNING after identity is proven; the durable handshake end."""
        now = self._clock.now_iso()
        if lease.pid is None:
            raise ConfigError(
                "WORKER_REGISTRATION_PID_MISSING: cannot complete a lease with no pid"
            )
        ready = _register_ready(
            lease,
            updated_at=now,
            process_identity=process_identity,
            pid=lease.pid,
        )
        envelope = self._leases.save(ready, expected_revision=expected_revision)
        self._mirror(ready, envelope.revision)
        running = _mark_running(ready, updated_at=now)
        envelope = self._leases.save(running, expected_revision=envelope.revision)
        self._mirror(running, envelope.revision)
        return running, envelope.revision

    def abort_intent(
        self,
        lease: ProcessLease,
        *,
        error_code: str,
        error_message: str,
        expected_revision: Revision,
    ) -> None:
        """Terminalize a REGISTERED intent that will never become a worker."""
        terminated = _abort_intent(
            lease,
            updated_at=self._clock.now_iso(),
            error_code=error_code,
            error_message=error_message,
        )
        envelope = self._leases.save(terminated, expected_revision=expected_revision)
        self._mirror(terminated, envelope.revision)

    def _mirror(self, lease: ProcessLease, revision: Revision) -> None:
        """Mirror one authoritative write into the shadow; never fail the write."""
        if self._shadow is None:
            return
        try:
            self._shadow.write_shadow(lease, revision)
        except Exception:
            return
