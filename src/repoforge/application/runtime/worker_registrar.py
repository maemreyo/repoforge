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

Admission fence (P1-3): ``create_intent`` refuses to open a spawn while the
durable admission epoch is closing -- a restarter that is about to stop the
incumbent has fenced admission, so a new intent can never slip in between a
preflight and a stop. The stamping epoch is recorded on the lease so the
restarter's fence scan can see which spawns belong to the current epoch.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Iterator
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
from ...ports.admission_epoch import ADMISSION_OPEN, ADMISSION_PERMIT_ENV, AdmissionEpochStore
from ...ports.clock import Clock
from ...ports.ids import IdGenerator
from ...ports.locking import LockManager
from ...ports.process_lease_store import LeaseShadowStore, ProcessLeaseStore
from ...ports.worker_registrar import WORKER_ADMISSION_LOCK

#: The release-identity env the launcher shim stamps on the process it serves; the
#: replacement permit is bound to this so a permit for one release cannot admit a
#: spawn for another.
_RUNNING_RELEASE_SHA_ENV = "REPOFORGE_RUNNING_RELEASE_SHA"


class WorkerRegistrar:
    """Orchestrate the pre-spawn intent -> running lease lifecycle."""

    def __init__(
        self,
        *,
        leases: ProcessLeaseStore,
        ids: IdGenerator,
        clock: Clock,
        shadow: LeaseShadowStore | None = None,
        epochs: AdmissionEpochStore | None = None,
        locks: LockManager | None = None,
        admission_timeout_seconds: float = 5.0,
    ) -> None:
        self._leases = leases
        self._ids = ids
        self._clock = clock
        self._shadow = shadow
        self._epochs = epochs
        self._locks = locks
        self._admission_timeout_seconds = max(0.0, admission_timeout_seconds)

    def create_intent(
        self, *, role: ProcessLeaseRole, correlation_id: str
    ) -> tuple[ProcessLease, Revision]:
        """Durably record the REGISTERED intent BEFORE any process is spawned.

        Returns the lease together with the revision this caller now owns; every
        later transition must CAS on that owned revision, never on a fresh store
        read, so a concurrent terminalize cannot be resurrected (F-001).

        Refuses to open a new spawn while admission is closing (P1-3): a restarter
        that has fenced the epoch must not have a fresh intent appear underneath
        its final preflight.

        The OPEN check and the intent create are one atomic section: the shared
        worker-admission lock is held across the read -> create window, so a
        restarter fencing the epoch in another process can never interleave
        between the OPEN read and the lease write (P1-3). A wedged holder
        surfaces a typed LOCK_TIMEOUT failure instead of an unbounded wait.
        """
        with self._admission_lock():
            epoch: int | None = None
            if self._epochs is not None:
                current, state = self._epochs.read()
                # F-012: admission is CLOSING for a handoff. The ONLY spawn allowed
                # is the replacement supervisor the restarter issued a single-use
                # permit to; everything else (the incumbent, an unrelated process,
                # or a stale permit from a previous handoff) is refused exactly as
                # before. The claim is atomic under the shared admission lock.
                if state != ADMISSION_OPEN and not self._claim_replacement_permit(current):
                    raise ConfigError(
                        "WORKER_ADMISSION_REFUSED: worker admission is fenced "
                        f"(epoch {current} is {state}); no valid replacement "
                        "permit for this spawn"
                    )
                epoch = current
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
                admission_epoch=epoch,
            )
            envelope = self._leases.create(lease)
            self._mirror(lease, envelope.revision)
            return lease, envelope.revision

    @contextlib.contextmanager
    def _admission_lock(self) -> Iterator[None]:
        """The shared worker-admission fence (P1-3), bounded.

        Every spawn path and every restarter synchronize on the SAME lock name and
        lock root, so the registrar's OPEN-check + intent-create and the restarter's
        fence -> final observation -> stop -> reopen are mutually exclusive. When no
        lock manager is wired (tests, minimal embedders) admission is unchecked --
        the caller opts out of cross-process fencing, never into a weaker fence.
        """
        if self._locks is None:
            yield
            return
        with self._locks.lock(
            WORKER_ADMISSION_LOCK,
            timeout_seconds=self._admission_timeout_seconds,
            metadata={"owner": "worker-registrar"},
        ):
            yield

    def _claim_replacement_permit(self, epoch: int) -> bool:
        """Claim the single-use replacement permit for the CLOSING handoff (F-012).

        The replacement supervisor carries the restarter-issued permit token in its
        environment; ``create_intent`` presents it with the release it actually
        serves. Any mismatch (no token, wrong token, wrong target, stale epoch, or
        an already-used permit) fails closed -- exactly like the pre-permit refusal
        -- so the incumbent and unrelated processes stay blocked while CLOSING.
        """
        token = os.environ.get(ADMISSION_PERMIT_ENV, "")
        if not token:
            return False
        epochs = self._epochs
        if epochs is None:
            return False
        target = os.environ.get(_RUNNING_RELEASE_SHA_ENV)
        try:
            return bool(epochs.claim_permit(epoch, token=token, target=target))
        except Exception:
            return False

    def record_pid(
        self,
        lease: ProcessLease,
        *,
        pid: int,
        expected_revision: Revision,
        pgid: int | None = None,
        process_start_token: str | None = None,
        owner_pid: int | None = None,
        owner_process_identity: str | None = None,
    ) -> tuple[ProcessLease, Revision]:
        """Persist the pid the instant Popen returns.

        This is the write that makes a post-crash process discoverable: the lease
        exists before the process, and the pid lands in it before any further work,
        so no crash point leaves a live process with neither a lease nor a pid.
        CAS uses the caller's owned revision; a stale writer fails instead of
        resurrecting a terminalized lease.

        The single-authority identity fields (pgid, start token) land here too, so
        the reconciler can prove the process from the lease alone. The owner
        fields land here as well (the supervisor already knows its own pid and
        identity at spawn time), so a parent that dies before
        ``complete_registration`` still leaves a lease self-sufficient for
        lease-only recovery -- the reconciler never needs the projection to know
        who owned the worker (F-001 P0).
        """
        updated = replace(
            lease,
            pid=pid,
            pgid=pgid if pgid is not None else lease.pgid,
            process_start_token=(
                process_start_token
                if process_start_token is not None
                else lease.process_start_token
            ),
            owner_pid=owner_pid if owner_pid is not None else lease.owner_pid,
            owner_process_identity=(
                owner_process_identity
                if owner_process_identity is not None
                else lease.owner_process_identity
            ),
            updated_at=self._clock.now_iso(),
        )
        envelope = self._leases.save(updated, expected_revision=expected_revision)
        self._mirror(updated, envelope.revision)
        return updated, envelope.revision

    def complete_registration(
        self,
        lease: ProcessLease,
        *,
        process_identity: str,
        expected_revision: Revision,
        owner_pid: int | None = None,
        owner_process_identity: str | None = None,
        release_sha: str | None = None,
        generation: int | None = None,
        process_start_token: str | None = None,
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
            pgid=lease.pgid,
            process_start_token=(
                process_start_token
                if process_start_token is not None
                else lease.process_start_token
            ),
        )
        ready = replace(
            ready,
            owner_pid=owner_pid if owner_pid is not None else lease.owner_pid,
            owner_process_identity=(
                owner_process_identity
                if owner_process_identity is not None
                else lease.owner_process_identity
            ),
            release_sha=release_sha if release_sha is not None else lease.release_sha,
            generation=generation if generation is not None else lease.generation,
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

    def claim_intent(
        self,
        lease_id: str,
        *,
        process_identity: str,
        pid: int,
        pgid: int | None = None,
        process_start_token: str | None = None,
        owner_pid: int | None = None,
        owner_process_identity: str | None = None,
    ) -> tuple[ProcessLease, Revision]:
        """Child-side claim of an abandoned REGISTERED intent (F-001 P0).

        The parent writes the REGISTERED intent before Popen and records the pid
        the moment Popen returns; if the parent dies between those writes the
        worker runs with a pid-less lease no reclaim path can find. The worker
        itself closes that window: it claims the intent here (REGISTERED ->
        READY with its own pid and identity), CAS-bound to the current revision
        so it never clobbers a parent that finished the handshake first.

        The claim stops at READY on purpose: the durable binding projection is
        written BETWEEN ``claim_intent`` and ``complete_claim``, so a crash after
        the claim leaves READY-with-pid -- a state a later reconciler can prove
        and reclaim from the canonical lease alone (it carries pid, pgid, start
        token, and the recorded owner), never a RUNNING lease with no projection
        the recovery path cannot reconstruct (F-001 P0).
        """
        envelope = self._leases.read(lease_id)
        if envelope is None:
            raise ConfigError(f"WORKER_CLAIM_LEASE_MISSING: no lease {lease_id!r} to claim")
        lease = envelope.value
        if lease.pid is not None and lease.pid != pid:
            raise ConfigError(
                "WORKER_CLAIM_PID_CONFLICT: "
                f"lease {lease_id!r} already names pid {lease.pid}, not {pid}"
            )
        if lease.status in {
            ProcessLeaseStatus.READY,
            ProcessLeaseStatus.RUNNING,
        }:
            return lease, envelope.revision
        now = self._clock.now_iso()
        ready = _register_ready(
            lease,
            updated_at=now,
            process_identity=process_identity,
            pid=pid,
            pgid=pgid if pgid is not None else lease.pgid,
            process_start_token=process_start_token,
        )
        ready = replace(
            ready,
            owner_pid=owner_pid if owner_pid is not None else lease.owner_pid,
            owner_process_identity=(
                owner_process_identity
                if owner_process_identity is not None
                else lease.owner_process_identity
            ),
        )
        saved = self._leases.save(ready, expected_revision=envelope.revision)
        self._mirror(saved.value, saved.revision)
        return saved.value, saved.revision

    def complete_claim(
        self,
        lease: ProcessLease,
        *,
        expected_revision: Revision,
    ) -> tuple[ProcessLease, Revision]:
        """Child-side READY -> RUNNING acknowledgement (F-001 P0).

        Runs only AFTER the durable binding projection exists, mirroring the
        parent flow (``record_pid`` -> binding -> ``complete_registration``). A
        crash between ``claim_intent`` and ``complete_claim`` leaves a
        READY-with-pid lease (with or without a binding); the containment scan
        treats a fence lease as an in-flight claim, never a split-brain.
        """
        if lease.status is ProcessLeaseStatus.RUNNING:
            return lease, expected_revision
        running = _mark_running(lease, updated_at=self._clock.now_iso())
        envelope = self._leases.save(running, expected_revision=expected_revision)
        self._mirror(running, envelope.revision)
        return running, envelope.revision

    def _mirror(self, lease: ProcessLease, revision: Revision) -> None:
        """Mirror one authoritative write into the shadow; never fail the write."""
        if self._shadow is None:
            return
        try:
            self._shadow.write_shadow(lease, revision)
        except Exception:
            return
