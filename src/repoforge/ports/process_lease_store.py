"""Persistence boundary for durable process-lease state."""

from __future__ import annotations

from typing import Protocol

from ..domain.durable_state import Revision, StateEnvelope, StatePage
from ..domain.process_lease import ProcessLease


class ProcessLeaseStore(Protocol):
    def create(self, lease: ProcessLease) -> StateEnvelope[ProcessLease]: ...

    def read(self, lease_id: str) -> StateEnvelope[ProcessLease] | None: ...

    def save(
        self,
        lease: ProcessLease,
        *,
        expected_revision: Revision,
    ) -> StateEnvelope[ProcessLease]: ...

    def list_all(self, *, max_records: int = 2_000) -> StatePage[ProcessLease]: ...

    def delete(self, lease_id: str, *, expected_revision: Revision) -> bool: ...
