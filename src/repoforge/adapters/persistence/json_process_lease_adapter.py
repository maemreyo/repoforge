"""Deferred — Phase 3 JSON persistence for ProcessLeaseStore."""

from __future__ import annotations

from ...domain.durable_state import Revision, StateEnvelope, StatePage
from ...domain.process_lease import ProcessLease
from ...ports.process_lease_store import ProcessLeaseStore


class JsonProcessLeaseAdapter(ProcessLeaseStore):
    """JSON-backed ProcessLeaseStore — deferred to Phase 3."""

    def create(self, lease: ProcessLease) -> StateEnvelope[ProcessLease]:
        raise NotImplementedError("JsonProcessLeaseAdapter deferred to Phase 3")

    def read(self, lease_id: str) -> StateEnvelope[ProcessLease] | None:
        raise NotImplementedError("JsonProcessLeaseAdapter deferred to Phase 3")

    def save(
        self,
        lease: ProcessLease,
        *,
        expected_revision: Revision,
    ) -> StateEnvelope[ProcessLease]:
        raise NotImplementedError("JsonProcessLeaseAdapter deferred to Phase 3")

    def list_all(self, *, max_records: int = 2_000) -> StatePage[ProcessLease]:
        raise NotImplementedError("JsonProcessLeaseAdapter deferred to Phase 3")

    def delete(self, lease_id: str, *, expected_revision: Revision) -> bool:
        raise NotImplementedError("JsonProcessLeaseAdapter deferred to Phase 3")
