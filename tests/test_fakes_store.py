"""Tests for ``InMemoryProcessLeaseStore`` and ``InMemoryRuntimeTransitionStore`` from fakes_ext."""

from __future__ import annotations

import pytest

from repoforge.domain.durable_state import Revision, StateEnvelope
from repoforge.domain.process_lease import (
    ProcessLease,
    ProcessLeaseStatus,
    register_ready,
)
from repoforge.domain.runtime_transition import (
    RuntimeTransition,
    RuntimeTransitionStatus,
    new_runtime_transition,
)
from repoforge.testing.fakes_ext import (
    InMemoryProcessLeaseStore,
    InMemoryRuntimeTransitionStore,
)

_LID = "lease-000000000000000000000001"
_TID = "tran-000000000000000000000001"
_TID2 = "tran-000000000000000000000002"
_ISO = "2026-01-01T00:00:00+00:00"


def _lease(**overrides: object) -> ProcessLease:
    """Build a minimal REGISTERED ProcessLease."""
    return ProcessLease(
        lease_id=_LID,
        status=ProcessLeaseStatus.REGISTERED,
        process_identity=None,
        pid=None,
        started_at=None,
        heartbeat_at=None,
        correlation_id="corr-001",
        created_at=_ISO,
        updated_at=_ISO,
    )


def _transition(**overrides: object) -> RuntimeTransition:
    """Build a minimal PREPARED RuntimeTransition."""
    return new_runtime_transition(
        transition_id=_TID,
        target_generation=1,
        correlation_id="corr-001",
        started_at=_ISO,
        **{
            k: v
            for k, v in overrides.items()
            if k in ("config_generation", "previous_transition_id")
        },
    )


# --------------------------------------------------------------------------- InMemoryProcessLeaseStore


class TestInMemoryProcessLeaseStore:
    """Given an InMemoryProcessLeaseStore, when performing CRUD operations, then they behave as expected."""

    def test_create_and_read_happy_path(self) -> None:
        """Given a store, when creating a lease and reading it, then the envelope contains the lease."""
        store = InMemoryProcessLeaseStore()
        lease = _lease()
        envelope = store.create(lease)
        assert isinstance(envelope, StateEnvelope)
        assert envelope.value.lease_id == _LID
        assert envelope.revision is not None

        read_back = store.read(_LID)
        assert read_back is not None
        assert read_back.value == lease

    def test_create_duplicate_raises(self) -> None:
        """Given a store with an existing lease, when creating a duplicate, then KeyError is raised."""
        store = InMemoryProcessLeaseStore()
        store.create(_lease())
        with pytest.raises(KeyError, match="already exists"):
            store.create(_lease())

    def test_read_non_existent_returns_none(self) -> None:
        """Given a store, when reading a non-existent lease_id, then None is returned."""
        store = InMemoryProcessLeaseStore()
        result = store.read("lease-ffffffffffffffffffffffff")
        assert result is None

    def test_save_with_correct_revision_succeeds(self) -> None:
        """Given a created lease, when saving with the correct expected_revision, then it returns a new envelope."""
        store = InMemoryProcessLeaseStore()
        lease = _lease()
        envelope = store.create(lease)

        # Transition the lease and save with correct revision
        ready = register_ready(
            lease,
            updated_at="2026-01-01T00:00:01+00:00",
            process_identity="tok-abc",
            pid=1001,
        )
        updated = store.save(ready, expected_revision=envelope.revision)
        assert isinstance(updated, StateEnvelope)
        assert updated.value.status is ProcessLeaseStatus.READY
        # Revision should have bumped
        assert updated.revision.value > envelope.revision.value

    def test_save_with_wrong_revision_raises(self) -> None:
        """Given a created lease, when saving with a wrong expected_revision, then ValueError is raised."""
        store = InMemoryProcessLeaseStore()
        lease = _lease()
        store.create(lease)

        bad_revision = Revision(999)
        with pytest.raises(ValueError, match="CAS mismatch"):
            store.save(lease, expected_revision=bad_revision)

    def test_save_non_existent_raises(self) -> None:
        """Given a store without the lease, when saving, then ValueError is raised."""
        store = InMemoryProcessLeaseStore()
        lease = _lease()
        with pytest.raises(ValueError, match="not found"):
            store.save(lease, expected_revision=Revision(1))

    def test_delete_with_correct_revision_succeeds(self) -> None:
        """Given a created lease, when deleting with the correct revision, then it returns True and the record is gone."""
        store = InMemoryProcessLeaseStore()
        envelope = store.create(_lease())
        result = store.delete(_LID, expected_revision=envelope.revision)
        assert result is True
        assert store.read(_LID) is None

    def test_delete_non_existent_returns_false(self) -> None:
        """Given a store, when deleting a non-existent lease_id, then it returns False."""
        store = InMemoryProcessLeaseStore()
        result = store.delete("lease-ffffffffffffffffffffffff", expected_revision=Revision(1))
        assert result is False

    def test_delete_with_wrong_revision_raises(self) -> None:
        """Given a created lease, when deleting with a wrong revision, then ValueError is raised."""
        store = InMemoryProcessLeaseStore()
        store.create(_lease())
        with pytest.raises(ValueError, match="CAS mismatch"):
            store.delete(_LID, expected_revision=Revision(999))

    def test_list_all_returns_sorted_records(self) -> None:
        """Given multiple leases, when listing all, then they are sorted by updated_at descending."""
        store = InMemoryProcessLeaseStore()
        store.create(_lease())
        lease2 = ProcessLease(
            lease_id="lease-000000000000000000000002",
            status=ProcessLeaseStatus.REGISTERED,
            process_identity=None,
            pid=None,
            started_at=None,
            heartbeat_at=None,
            correlation_id="corr-002",
            created_at=_ISO,
            updated_at="2026-01-01T00:00:02+00:00",
        )
        store.create(lease2)
        page = store.list_all()
        assert len(page.records) == 2
        # Most recent first
        assert page.records[0].value.lease_id == "lease-000000000000000000000002"

    def test_list_all_respects_max_records(self) -> None:
        """Given more leases than max_records, when listing all, then scan_truncated is True."""
        store = InMemoryProcessLeaseStore()
        for i in range(5):
            lid = f"lease-{i:024x}"
            store.create(
                ProcessLease(
                    lease_id=lid,
                    status=ProcessLeaseStatus.REGISTERED,
                    process_identity=None,
                    pid=None,
                    started_at=None,
                    heartbeat_at=None,
                    correlation_id=f"corr-{i}",
                    created_at=_ISO,
                    updated_at=_ISO,
                )
            )
        page = store.list_all(max_records=3)
        assert len(page.records) == 3
        assert page.scan_truncated is True


# --------------------------------------------------------------------------- InMemoryRuntimeTransitionStore


class TestInMemoryRuntimeTransitionStore:
    """Given an InMemoryRuntimeTransitionStore, when performing CRUD operations, then they behave as expected."""

    def test_create_and_read_happy_path(self) -> None:
        """Given a store, when creating a transition and reading it, then the envelope matches."""
        store = InMemoryRuntimeTransitionStore()
        t = _transition()
        envelope = store.create(t)
        assert isinstance(envelope, StateEnvelope)
        assert envelope.value.transition_id == _TID

        read_back = store.read(_TID)
        assert read_back is not None
        assert read_back.value == t

    def test_create_duplicate_raises(self) -> None:
        """Given a store with an existing transition, when creating a duplicate, then KeyError is raised."""
        store = InMemoryRuntimeTransitionStore()
        store.create(_transition())
        with pytest.raises(KeyError, match="already exists"):
            store.create(_transition())

    def test_read_non_existent_returns_none(self) -> None:
        """Given a store, when reading a non-existent transition_id, then None is returned."""
        store = InMemoryRuntimeTransitionStore()
        result = store.read("tran-ffffffffffffffffffffffff")
        assert result is None

    def test_save_with_correct_revision_succeeds(self) -> None:
        """Given a created transition, when saving with the correct expected_revision, then it succeeds."""
        store = InMemoryRuntimeTransitionStore()
        t = _transition()
        envelope = store.create(t)
        # Advance the transition
        t2 = t.config_generated(updated_at="2026-01-01T00:00:01+00:00")
        updated = store.save(t2, expected_revision=envelope.revision)
        assert updated.value.status is RuntimeTransitionStatus.CONFIG_GENERATED

    def test_save_with_wrong_revision_raises(self) -> None:
        """Given a created transition, when saving with a wrong expected_revision, then ValueError is raised."""
        store = InMemoryRuntimeTransitionStore()
        store.create(_transition())
        with pytest.raises(ValueError, match="CAS mismatch"):
            store.save(_transition(), expected_revision=Revision(999))

    def test_save_non_existent_raises(self) -> None:
        """Given a store without the transition, when saving, then ValueError is raised."""
        store = InMemoryRuntimeTransitionStore()
        with pytest.raises(ValueError, match="not found"):
            store.save(_transition(), expected_revision=Revision(1))

    def test_list_by_generation_filters(self) -> None:
        """Given transitions with different target_generation, when listing by generation, then only matching ones are returned."""
        store = InMemoryRuntimeTransitionStore()
        t1 = _transition()  # target_generation=1
        store.create(t1)

        t2 = new_runtime_transition(
            transition_id=_TID2,
            target_generation=2,
            correlation_id="corr-002",
            started_at=_ISO,
        )
        store.create(t2)

        page = store.list_by_generation(1)
        assert len(page.records) == 1
        assert page.records[0].value.transition_id == _TID

    def test_list_by_generation_empty(self) -> None:
        """Given no transitions match the generation, when listing, then an empty page is returned."""
        store = InMemoryRuntimeTransitionStore()
        page = store.list_by_generation(99)
        assert len(page.records) == 0
        assert page.scan_truncated is False

    def test_list_by_generation_respects_max_records(self) -> None:
        """Given more transitions than max_records, when listing by generation, then scan_truncated is True."""
        store = InMemoryRuntimeTransitionStore()
        for i in range(5):
            tid = f"tran-{i + 1:024x}"
            t = new_runtime_transition(
                transition_id=tid,
                target_generation=1,
                correlation_id=f"corr-{i}",
                started_at=_ISO,
            )
            store.create(t)
        page = store.list_by_generation(1, max_records=3)
        assert len(page.records) == 3
        assert page.scan_truncated is True

    def test_list_all_returns_all(self) -> None:
        """Given two transitions, when listing all, both are returned."""
        store = InMemoryRuntimeTransitionStore()
        t1 = _transition()
        store.create(t1)
        t2 = new_runtime_transition(
            transition_id=_TID2,
            target_generation=2,
            correlation_id="corr-002",
            started_at=_ISO,
        )
        store.create(t2)
        page = store.list_all()
        assert len(page.records) == 2
