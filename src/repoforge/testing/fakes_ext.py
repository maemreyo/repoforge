"""In-memory ProcessLeaseStore and RuntimeTransitionStore fakes — dict + Revision CAS."""

from __future__ import annotations

from ..domain.durable_state import Revision, SchemaVersion, StateEnvelope, StatePage
from ..domain.process_lease import ACTIVE_LEASE_STATUSES, ProcessLease, ProcessLeaseStatus
from ..domain.runtime_transition import RuntimeTransition
from ..ports.process_lease_store import ProcessLeasePage, ProcessLeaseStore  # noqa: F401
from ..ports.runtime_transition_store import RuntimeTransitionStore  # noqa: F401

__all__ = [
    "InMemoryProcessLeaseStore",
    "InMemoryRuntimeTransitionStore",
]

_SCHEMA_V1 = SchemaVersion(1)


class InMemoryProcessLeaseStore:
    """In-memory ProcessLeaseStore backed by a dict with revision-based CAS."""

    def __init__(self) -> None:
        self._records: dict[str, tuple[ProcessLease, Revision]] = {}
        self._archived: dict[str, tuple[ProcessLease, Revision]] = {}
        self._next_revision: int = 1

    def _next(self) -> Revision:
        value = self._next_revision
        self._next_revision += 1
        return Revision(value)

    def _envelope(self, lease: ProcessLease, revision: Revision) -> StateEnvelope[ProcessLease]:
        return StateEnvelope(
            record_id=lease.lease_id,
            schema_version=_SCHEMA_V1,
            revision=revision,
            value=lease,
        )

    def create(self, lease: ProcessLease) -> StateEnvelope[ProcessLease]:
        if lease.lease_id in self._records:
            raise KeyError(f"Process lease already exists: {lease.lease_id}")
        revision = self._next()
        self._records[lease.lease_id] = (lease, revision)
        return self._envelope(lease, revision)

    def read(self, lease_id: str) -> StateEnvelope[ProcessLease] | None:
        data = self._records.get(lease_id)
        return None if data is None else self._envelope(*data)

    def save(
        self, lease: ProcessLease, *, expected_revision: Revision
    ) -> StateEnvelope[ProcessLease]:
        data = self._records.get(lease.lease_id)
        if data is None:
            raise ValueError(f"Process lease not found: {lease.lease_id}")
        _current, current_revision = data
        if current_revision != expected_revision:
            raise ValueError(
                f"CAS mismatch for {lease.lease_id}: "
                f"expected {expected_revision.value}, got {current_revision.value}"
            )
        new_revision = self._next()
        self._records[lease.lease_id] = (lease, new_revision)
        return self._envelope(lease, new_revision)

    def list_all(self, *, max_records: int = 2_000) -> StatePage[ProcessLease]:
        all_records = sorted(
            self._records.values(),
            key=lambda item: item[0].updated_at,
            reverse=True,
        )
        page = all_records[:max_records]
        return StatePage(
            records=tuple(self._envelope(*item) for item in page),
            scan_truncated=len(all_records) > max_records,
        )

    def list_page(
        self,
        *,
        role: object | None = None,
        max_records: int = 2_000,
    ) -> ProcessLeasePage:
        from ..domain.process_lease import ProcessLeaseRole

        records = tuple(
            lease
            for lease, _revision in self._records.values()
            if role is None or lease.role is role or lease.role is ProcessLeaseRole(role)
        )
        return self._page(records, max_records=max_records)

    def list_active_page(
        self,
        *,
        role: object | None = None,
        max_records: int = 2_000,
    ) -> ProcessLeasePage:
        from ..domain.process_lease import ProcessLeaseRole

        records = tuple(
            lease
            for lease, _revision in self._records.values()
            if lease.status in ACTIVE_LEASE_STATUSES
            and (role is None or lease.role is role or lease.role is ProcessLeaseRole(role))
        )
        return self._page(records, max_records=max_records)

    def collect_terminal(self, *, max_records: int = 5_000) -> int:
        terminal = [
            lease_id
            for lease_id, (lease, _revision) in self._records.items()
            if lease.status in {ProcessLeaseStatus.TERMINATED, ProcessLeaseStatus.ARCHIVED}
        ]
        removed = 0
        for lease_id in terminal[:max_records]:
            lease, revision = self._records.pop(lease_id)
            self._archived[lease_id] = (lease, revision)
            removed += 1
        return removed

    @staticmethod
    def _page(records: tuple[ProcessLease, ...], *, max_records: int) -> ProcessLeasePage:
        ordered = tuple(sorted(records, key=lambda lease: lease.lease_id))
        return ProcessLeasePage(
            records=ordered[:max_records],
            scan_complete=len(ordered) <= max_records,
            unreadable_ids=(),
        )

    def delete(self, lease_id: str, *, expected_revision: Revision) -> bool:
        data = self._records.get(lease_id)
        if data is None:
            return False
        _lease, current_revision = data
        if current_revision != expected_revision:
            raise ValueError(
                f"CAS mismatch for {lease_id}: "
                f"expected {expected_revision.value}, got {current_revision.value}"
            )
        del self._records[lease_id]
        return True


class InMemoryRuntimeTransitionStore:
    """In-memory RuntimeTransitionStore backed by a dict with revision-based CAS."""

    def __init__(self) -> None:
        self._records: dict[str, tuple[RuntimeTransition, Revision]] = {}
        self._next_revision: int = 1

    def _next(self) -> Revision:
        value = self._next_revision
        self._next_revision += 1
        return Revision(value)

    def _envelope(
        self, transition: RuntimeTransition, revision: Revision
    ) -> StateEnvelope[RuntimeTransition]:
        return StateEnvelope(
            record_id=transition.transition_id,
            schema_version=_SCHEMA_V1,
            revision=revision,
            value=transition,
        )

    def create(self, transition: RuntimeTransition) -> StateEnvelope[RuntimeTransition]:
        if transition.transition_id in self._records:
            raise KeyError(f"Runtime transition already exists: {transition.transition_id}")
        revision = self._next()
        self._records[transition.transition_id] = (transition, revision)
        return self._envelope(transition, revision)

    def read(self, transition_id: str) -> StateEnvelope[RuntimeTransition] | None:
        data = self._records.get(transition_id)
        return None if data is None else self._envelope(*data)

    def save(
        self, transition: RuntimeTransition, *, expected_revision: Revision
    ) -> StateEnvelope[RuntimeTransition]:
        data = self._records.get(transition.transition_id)
        if data is None:
            raise ValueError(f"Runtime transition not found: {transition.transition_id}")
        _current, current_revision = data
        if current_revision != expected_revision:
            raise ValueError(
                f"CAS mismatch for {transition.transition_id}: "
                f"expected {expected_revision.value}, got {current_revision.value}"
            )
        new_revision = self._next()
        self._records[transition.transition_id] = (transition, new_revision)
        return self._envelope(transition, new_revision)

    def list_all(self, *, max_records: int = 2_000) -> StatePage[RuntimeTransition]:
        all_records = sorted(
            self._records.values(),
            key=lambda item: item[0].updated_at,
            reverse=True,
        )
        page = all_records[:max_records]
        return StatePage(
            records=tuple(self._envelope(*item) for item in page),
            scan_truncated=len(all_records) > max_records,
        )

    def list_by_generation(
        self, generation: int, *, max_records: int = 100
    ) -> StatePage[RuntimeTransition]:
        matching = [
            (txn, rev) for txn, rev in self._records.values() if txn.target_generation == generation
        ]
        sorted_matching = sorted(matching, key=lambda item: item[0].updated_at, reverse=True)
        page = sorted_matching[:max_records]
        return StatePage(
            records=tuple(self._envelope(*item) for item in page),
            scan_truncated=len(sorted_matching) > max_records,
        )

    def get_active_by_correlation(
        self, correlation_id: str, *, max_records: int = 100
    ) -> StateEnvelope[RuntimeTransition] | None:
        from ..domain.errors import ConfigError
        from ..domain.runtime_transition import is_terminal, is_terminal_failure

        matching = [
            self._envelope(txn, rev)
            for txn, rev in self._records.values()
            if txn.correlation_id == correlation_id
            and not is_terminal(txn.status)
            and not is_terminal_failure(txn.status)
        ]
        matching.sort(key=lambda item: item.record_id)
        if len(matching) > 1:
            raise ConfigError(
                "RUNTIME_TRANSITION_INVARIANT_VIOLATED: more than one non-terminal "
                f"transition for correlation {correlation_id} ("
                + ", ".join(item.record_id for item in matching[:8])
                + ")"
            )
        page = matching[:max_records]
        return page[0] if page else None
