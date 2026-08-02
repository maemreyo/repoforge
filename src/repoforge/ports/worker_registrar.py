"""Durable worker-registration boundary: pre-spawn intent -> running lease."""

from __future__ import annotations

from typing import Protocol

from ..domain.process_lease import ProcessLease, ProcessLeaseRole


class WorkerRegistrar(Protocol):
    """The pre-spawn intent -> RUNNING lease lifecycle a worker spawn must follow.

    ``create_intent`` is called BEFORE any process exists; ``record_pid`` the moment
    a spawned pid is known; ``complete_registration`` only after the process
    identity is proven, leaving the lease durably RUNNING. ``abort_intent``
    terminalizes a pre-spawn lease that will never become a worker.
    """

    def create_intent(self, *, role: ProcessLeaseRole, correlation_id: str) -> ProcessLease: ...

    def record_pid(self, lease: ProcessLease, *, pid: int) -> ProcessLease: ...

    def complete_registration(
        self,
        lease: ProcessLease,
        *,
        process_identity: str,
    ) -> ProcessLease: ...

    def abort_intent(
        self,
        lease: ProcessLease,
        *,
        error_code: str,
        error_message: str,
    ) -> None: ...
