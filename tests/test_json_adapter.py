"""JSON ProcessLeaseAdapter and RuntimeTransitionAdapter (both real, Phase 4/5)."""

from __future__ import annotations

from pathlib import Path

import pytest

from repoforge.adapters.persistence.json_process_lease_adapter import (
    JsonProcessLeaseAdapter,
    validate_lease_id,
)
from repoforge.adapters.persistence.json_runtime_transition_adapter import (
    JsonRuntimeTransitionAdapter,
    validate_transition_id,
)
from repoforge.domain.durable_state import Revision
from repoforge.domain.process_lease import (
    ProcessLease,
    ProcessLeaseRole,
    ProcessLeaseStatus,
    process_lease_from_payload,
    process_lease_payload,
)
from repoforge.domain.runtime_transition import (
    RuntimeTransition,
    new_runtime_transition,
)
from repoforge.testing import InMemoryLockManager

_LID = "lease-000000000000000000000001"
_TID = "tran-000000000000000000000001"
_ISO = "2026-01-01T00:00:00+00:00"


def _lease() -> ProcessLease:
    return ProcessLease(
        lease_id=_LID,
        status=ProcessLeaseStatus.REGISTERED,
        role=ProcessLeaseRole.EXECUTION_DAEMON,
        process_identity=None,
        pid=None,
        started_at=None,
        heartbeat_at=None,
        correlation_id="corr-001",
        created_at=_ISO,
        updated_at=_ISO,
    )


def _transition() -> RuntimeTransition:
    return new_runtime_transition(
        transition_id=_TID,
        target_generation=1,
        correlation_id="corr-001",
        started_at=_ISO,
    )


# --------------------------------------------------------------------------- JsonProcessLeaseAdapter


class TestJsonProcessLeaseAdapter:
    def test_create_and_read_round_trip(self, tmp_path: Path) -> None:
        adapter = JsonProcessLeaseAdapter(tmp_path / "state", InMemoryLockManager())
        envelope = adapter.create(_lease())

        assert envelope.revision == Revision(1)
        stored = adapter.read(_LID)
        assert stored is not None and stored.value == _lease()

    def test_create_then_save_with_cas(self, tmp_path: Path) -> None:
        adapter = JsonProcessLeaseAdapter(tmp_path / "state", InMemoryLockManager())
        envelope = adapter.create(_lease())
        running = ProcessLease(
            lease_id=_LID,
            status=ProcessLeaseStatus.RUNNING,
            role=ProcessLeaseRole.EXECUTION_DAEMON,
            process_identity="tok",
            pid=4242,
            started_at=_ISO,
            heartbeat_at=_ISO,
            correlation_id="corr-001",
            created_at=_ISO,
            updated_at=_ISO,
        )

        updated = adapter.save(running, expected_revision=envelope.revision)

        assert updated.revision == Revision(2)
        assert adapter.read(_LID).value.status is ProcessLeaseStatus.RUNNING

    def test_save_rejects_a_stale_revision(self, tmp_path: Path) -> None:
        adapter = JsonProcessLeaseAdapter(tmp_path / "state", InMemoryLockManager())
        adapter.create(_lease())

        from repoforge.domain.errors import RepoForgeError

        with pytest.raises(RepoForgeError):
            adapter.save(_lease(), expected_revision=Revision(99))

    def test_delete_only_when_revision_matches(self, tmp_path: Path) -> None:
        adapter = JsonProcessLeaseAdapter(tmp_path / "state", InMemoryLockManager())
        envelope = adapter.create(_lease())

        assert adapter.delete(_LID, expected_revision=Revision(99)) is False
        assert adapter.read(_LID) is not None
        assert adapter.delete(_LID, expected_revision=envelope.revision) is True
        assert adapter.read(_LID) is None

    def test_list_all_returns_every_lease(self, tmp_path: Path) -> None:
        adapter = JsonProcessLeaseAdapter(tmp_path / "state", InMemoryLockManager())
        adapter.create(_lease())
        adapter.create(
            ProcessLease(
                lease_id="lease-000000000000000000000002",
                status=ProcessLeaseStatus.RUNNING,
                role=ProcessLeaseRole.OPERATION_WORKER,
                process_identity="tok",
                pid=4242,
                started_at=_ISO,
                heartbeat_at=_ISO,
                correlation_id="corr-001",
                created_at=_ISO,
                updated_at=_ISO,
            )
        )

        page = adapter.list_all()

        assert len(page.records) == 2

    def test_list_page_filters_by_role(self, tmp_path: Path) -> None:
        adapter = JsonProcessLeaseAdapter(tmp_path / "state", InMemoryLockManager())
        adapter.create(_lease())
        adapter.create(
            ProcessLease(
                lease_id="lease-000000000000000000000002",
                status=ProcessLeaseStatus.RUNNING,
                role=ProcessLeaseRole.OPERATION_WORKER,
                process_identity="tok",
                pid=4242,
                started_at=_ISO,
                heartbeat_at=_ISO,
                correlation_id="corr-001",
                created_at=_ISO,
                updated_at=_ISO,
            )
        )

        page = adapter.list_page(role=ProcessLeaseRole.OPERATION_WORKER)

        assert [item.lease_id for item in page.records] == ["lease-000000000000000000000002"]


class TestProcessLeasePayload:
    def test_payload_round_trip_preserves_enums(self) -> None:
        lease = _lease()
        decoded = process_lease_from_payload(process_lease_payload(lease))

        assert decoded == lease
        assert decoded.status is ProcessLeaseStatus.REGISTERED
        assert decoded.role is ProcessLeaseRole.EXECUTION_DAEMON

    def test_from_payload_rejects_an_unknown_status(self) -> None:
        payload = process_lease_payload(_lease())
        payload["status"] = "mystery"

        with pytest.raises(ValueError):
            process_lease_from_payload(payload)


class TestLeaseIdValidation:
    def test_accepts_kind_hex_ids(self) -> None:
        assert (
            validate_lease_id("worker-000000000000000000000001")
            == "worker-000000000000000000000001"
        )
        assert validate_lease_id("lease-abcdef") == "lease-abcdef"

    def test_rejects_a_malformed_id(self) -> None:
        with pytest.raises(ValueError):
            validate_lease_id("no-dash-here")
        with pytest.raises(ValueError):
            validate_lease_id("worker-NotHex")


# --------------------------------------------------------------------------- JsonRuntimeTransitionAdapter


class TestTransitionPayload:
    def test_payload_round_trip_preserves_status(self) -> None:
        transition = _transition()
        from repoforge.domain.runtime_transition import (
            runtime_transition_from_payload,
            runtime_transition_payload,
        )

        decoded = runtime_transition_from_payload(runtime_transition_payload(transition))

        assert decoded == transition

    def test_from_payload_rejects_an_unknown_status(self) -> None:
        from repoforge.domain.runtime_transition import (
            runtime_transition_from_payload,
            runtime_transition_payload,
        )

        payload = runtime_transition_payload(_transition())
        payload["status"] = "mystery"

        with pytest.raises(ValueError):
            runtime_transition_from_payload(payload)


class TestTransitionIdValidation:
    def test_accepts_a_valid_transition_id(self) -> None:
        assert (
            validate_transition_id("tran-000000000000000000000001")
            == "tran-000000000000000000000001"
        )

    def test_rejects_a_malformed_id(self) -> None:
        with pytest.raises(ValueError):
            validate_transition_id("tran-not-hex")
        with pytest.raises(ValueError):
            validate_transition_id("worker-000000000000000000000001")


class TestJsonRuntimeTransitionAdapter:
    def test_create_and_read_round_trip(self, tmp_path: Path) -> None:
        adapter = JsonRuntimeTransitionAdapter(tmp_path / "state", InMemoryLockManager())
        envelope = adapter.create(_transition())

        assert envelope.revision == Revision(1)
        stored = adapter.read(_TID)
        assert stored is not None and stored.value == _transition()

    def test_save_advances_with_cas(self, tmp_path: Path) -> None:
        adapter = JsonRuntimeTransitionAdapter(tmp_path / "state", InMemoryLockManager())
        envelope = adapter.create(_transition())
        advanced = (
            _transition()
            .config_generated(updated_at=_ISO)
            .resolve_dependencies(updated_at=_ISO)
            .validate(updated_at=_ISO)
            .stage(updated_at=_ISO)
            .activate(updated_at=_ISO)
            .health_checked(updated_at=_ISO)
            .mark_ready(updated_at=_ISO)
        )

        updated = adapter.save(advanced, expected_revision=envelope.revision)

        assert updated.revision == Revision(2)
        from repoforge.domain.runtime_transition import RuntimeTransitionStatus

        assert adapter.read(_TID).value.status is RuntimeTransitionStatus.READY

    def test_save_rejects_a_stale_revision(self, tmp_path: Path) -> None:
        adapter = JsonRuntimeTransitionAdapter(tmp_path / "state", InMemoryLockManager())
        adapter.create(_transition())

        from repoforge.domain.errors import RepoForgeError

        with pytest.raises(RepoForgeError):
            adapter.save(_transition(), expected_revision=Revision(99))

    def test_list_all_returns_every_transition(self, tmp_path: Path) -> None:
        adapter = JsonRuntimeTransitionAdapter(tmp_path / "state", InMemoryLockManager())
        adapter.create(_transition())
        adapter.create(
            new_runtime_transition(
                transition_id="tran-000000000000000000000002",
                target_generation=2,
                correlation_id="corr-001",
                started_at=_ISO,
            )
        )

        page = adapter.list_all()

        assert len(page.records) == 2

    def test_list_by_generation_filters_on_the_target(self, tmp_path: Path) -> None:
        adapter = JsonRuntimeTransitionAdapter(tmp_path / "state", InMemoryLockManager())
        adapter.create(_transition())  # target_generation=1
        adapter.create(
            new_runtime_transition(
                transition_id="tran-000000000000000000000002",
                target_generation=2,
                correlation_id="corr-001",
                started_at=_ISO,
            )
        )

        page = adapter.list_by_generation(2)

        assert [item.record_id for item in page.records] == ["tran-000000000000000000000002"]


# ----------------------------------------------------------------------- SqliteTransitionStore


class TestSqliteTransitionStore:
    """The SQLite transition shadow mirrors authoritative JSON writes."""

    def test_write_shadow_and_list_shadow_round_trip(self, tmp_path: Path) -> None:
        from repoforge.adapters.persistence.sqlite_transition_store import (
            SqliteTransitionStore,
        )

        store = SqliteTransitionStore(tmp_path / "shadow.db")
        try:
            store.write_shadow(_transition(), Revision(1))

            entries = store.list_shadow()

            assert entries == [(_transition(), Revision(1))]
        finally:
            store.close()

    def test_write_shadow_upserts_and_delete_removes(self, tmp_path: Path) -> None:
        from repoforge.adapters.persistence.sqlite_transition_store import (
            SqliteTransitionStore,
        )

        store = SqliteTransitionStore(tmp_path / "shadow.db")
        try:
            store.write_shadow(_transition(), Revision(1))
            store.write_shadow(_transition(), Revision(2))

            entries = store.list_shadow()
            assert entries == [(_transition(), Revision(2))]

            store.delete_shadow(_TID)

            assert store.list_shadow() == []
        finally:
            store.close()
