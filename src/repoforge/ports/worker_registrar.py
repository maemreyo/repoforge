"""Durable worker-registration boundary: pre-spawn intent -> running lease."""

from __future__ import annotations

from typing import Protocol

from ..domain.durable_state import Revision
from ..domain.process_lease import ProcessLease, ProcessLeaseRole


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
    ) -> tuple[ProcessLease, Revision]: ...

    def complete_registration(
        self,
        lease: ProcessLease,
        *,
        process_identity: str,
        expected_revision: Revision,
    ) -> tuple[ProcessLease, Revision]: ...

    def abort_intent(
        self,
        lease: ProcessLease,
        *,
        error_code: str,
        error_message: str,
        expected_revision: Revision,
    ) -> None: ...
