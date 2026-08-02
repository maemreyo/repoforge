"""Persistence boundary for durable process-lease state."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ..domain.durable_state import Revision, StateEnvelope, StatePage
from ..domain.process_lease import ProcessLease, ProcessLeaseRole


@dataclass(frozen=True, slots=True)
class ProcessLeasePage:
    """A bounded registry scan with the completeness evidence a reconciler needs.

    ``scan_complete`` is False when the registry held more leases than the scan
    bound, and ``unreadable_ids`` names records that could not be decoded. A
    reconciler must fail closed on either -- an orphan past the limit or behind an
    unreadable record is invisible (F-008), so the caller can never mistake a
    partial view for the whole registry.
    """

    records: tuple[ProcessLease, ...]
    scan_complete: bool
    unreadable_ids: tuple[str, ...]


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

    def list_page(
        self,
        *,
        role: ProcessLeaseRole | None = None,
        max_records: int = 2_000,
    ) -> ProcessLeasePage: ...
