"""Persistence boundary for durable process-lease state."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ..domain.durable_state import Revision, StateEnvelope, StatePage
from ..domain.process_lease import ProcessLease, ProcessLeaseRole


@dataclass(frozen=True, slots=True)
class ProcessLeasePage:
    """A bounded registry scan with the completeness evidence a reconciler needs.

    ``scan_complete`` is False when the registry held more *active* leases than the
    scan bound, and ``unreadable_ids`` names records that could not be decoded. A
    reconciler must fail closed on either -- an orphan past the limit or behind an
    unreadable record is invisible (F-008), so the caller can never mistake a
    partial view for the whole registry.

    Terminal history never counts toward ``scan_complete``: an active scan must not
    become a permanent fail-closed block merely because 2,001 terminal records
    exist (active/terminal separation).
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

    def list_active_page(
        self,
        *,
        role: ProcessLeaseRole | None = None,
        max_records: int = 2_000,
    ) -> ProcessLeasePage:
        """A bounded scan of *active* leases only.

        Terminal leases (TERMINATED/ARCHIVED) are excluded from the scan and never
        count toward ``scan_complete``. A read-only preflight therefore cannot be
        blocked by accumulated terminal history.
        """
        ...

    def collect_terminal(self, *, max_records: int = 5_000) -> int:
        """Archive terminal leases out of the active scan (mutating maintenance).

        Returns the number of records physically moved. Read-only preflights never
        call this; the mutating reconciliation pass does, so the active scan stays
        small and terminal history lives in a separate immutable collection.
        """
        ...

    def archive_terminal(self, lease_id: str, *, expected_revision: Revision) -> bool:
        """Archive+delete ONE terminal record, CAS-bound to ``expected_revision``.

        Moves a TERMINATED/ARCHIVED record into the durable history collection and
        removes it from the active scan. Refuses to archive a live (non-terminal)
        record, and no-ops (returns False) when a concurrent writer has moved the
        record since the caller read it -- the caller leaves it for the next
        ``collect_terminal`` pass instead of deleting someone else's state.
        """
        ...


class LeaseShadowStore(Protocol):
    """Write-only mirror for parity checking, never authoritative.

    The authoritative lease writes land in the JSON store; the SQLite shadow is a
    replica so an operator can diff the two. ``write_shadow`` mirrors one
    authoritative write. The shadow must never be able to refuse an authoritative
    write -- the registrar swallows a shadow failure (F-001).

    ``delete_shadow`` mirrors a terminal archive on the authoritative side, so a
    normal termination cannot leave a stale TERMINATED shadow row that makes
    ``compare_lease_parity`` report ``only_in_shadow`` forever. ``collect_terminal``
    is the bounded maintenance sweep that converges rows the delete missed (a
    crash between archive and delete); both are best-effort and never able to fail
    an authoritative write.
    """

    def write_shadow(self, lease: ProcessLease, revision: Revision) -> None: ...

    def delete_shadow(self, lease_id: str) -> None:
        """Remove one lease from the shadow store (mirrors a terminal archive)."""
        ...

    def collect_terminal(self, *, max_records: int = 5_000) -> int:
        """Remove terminal shadow rows so the parity diff converges; returns count."""
        ...
