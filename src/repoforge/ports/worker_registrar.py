"""Durable worker-registration boundary: pre-spawn intent -> running lease."""

from __future__ import annotations

from typing import Protocol

from ..domain.durable_state import Revision
from ..domain.process_lease import ProcessLease, ProcessLeaseRole

#: The one worker-admission fence every spawn path and every restarter shares.
#: Held across the ENTIRE spawn transaction (create_intent -> record_pid ->
#: complete_registration), and held by a restarter across final observation ->
#: stop -> reclaim, so no spawn can slip in between a preflight and an incumbent
#: stop (P1-3). Bounded everywhere it is acquired: a wedged holder must surface a
#: typed fail-closed outcome, never an unbounded wait.
WORKER_ADMISSION_LOCK = "worker-admission"


class WorkerRegistrar(Protocol):
    """The pre-spawn intent -> RUNNING lease lifecycle a worker spawn must follow.

    ``create_intent`` is called BEFORE any process exists; ``record_pid`` the moment
    a spawned pid is known; ``complete_registration`` only after the process
    identity is proven, leaving the lease durably RUNNING. ``abort_intent``
    terminalizes a pre-spawn lease that will never become a worker.

    Every step returns or takes the ``Revision`` its caller owns: the registrar
    never re-reads the store's latest revision to bless a stale snapshot, so a
    recovery process that terminalized the lease in between cannot have it
    resurrected by a stale writer (compare-and-swap on the owned revision).
    """

    def create_intent(
        self, *, role: ProcessLeaseRole, correlation_id: str
    ) -> tuple[ProcessLease, Revision]: ...

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
    ) -> tuple[ProcessLease, Revision]: ...

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
    ) -> tuple[ProcessLease, Revision]: ...

    def abort_intent(
        self,
        lease: ProcessLease,
        *,
        error_code: str,
        error_message: str,
        expected_revision: Revision,
    ) -> None: ...

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

        Only the worker itself calls this, and only when its supervisor is
        provably dead: the parent crashed between ``create_intent`` and
        ``record_pid``/``complete_registration``, leaving a pid-less lease. The
        claim advances REGISTERED -> READY with the worker's own pid and
        identity, CAS-bound to the current revision, so a parent that did finish
        the handshake is never clobbered.

        The claim deliberately stops at READY: the durable binding projection is
        written BETWEEN ``claim_intent`` and ``complete_claim``, so a crash after
        the claim leaves READY-with-pid, which a later reconciler can prove and
        reclaim from the canonical lease alone. Recovery never depends on the
        projection (F-001 P0).
        """

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
        READY-with-pid lease with (or without) a binding; the containment scan
        treats a fence lease as an in-flight claim, never a split-brain.
        """
        ...
