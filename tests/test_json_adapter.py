"""Tests for the JSON ProcessLeaseAdapter and RuntimeTransitionAdapter —
both are declared as stubs that raise NotImplementedError on every method."""

from __future__ import annotations

import pytest

from repoforge.adapters.persistence.json_process_lease_adapter import (
    JsonProcessLeaseAdapter,
)
from repoforge.adapters.persistence.json_runtime_transition_adapter import (
    JsonRuntimeTransitionAdapter,
)
from repoforge.domain.durable_state import Revision
from repoforge.domain.process_lease import (
    ProcessLease,
    ProcessLeaseStatus,
)
from repoforge.domain.runtime_transition import (
    RuntimeTransition,
    new_runtime_transition,
)

_LID = "lease-000000000000000000000001"
_TID = "tran-000000000000000000000001"
_ISO = "2026-01-01T00:00:00+00:00"


def _lease() -> ProcessLease:
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


def _transition() -> RuntimeTransition:
    return new_runtime_transition(
        transition_id=_TID,
        target_generation=1,
        correlation_id="corr-001",
        started_at=_ISO,
    )


# --------------------------------------------------------------------------- JsonProcessLeaseAdapter


class TestJsonProcessLeaseAdapter:
    """Given a JsonProcessLeaseAdapter, every method raises NotImplementedError (deferred to Phase 3)."""

    def test_create_raises_not_implemented(self) -> None:
        adapter = JsonProcessLeaseAdapter()
        with pytest.raises(NotImplementedError):
            adapter.create(_lease())

    def test_read_raises_not_implemented(self) -> None:
        adapter = JsonProcessLeaseAdapter()
        with pytest.raises(NotImplementedError):
            adapter.read(_LID)

    def test_save_raises_not_implemented(self) -> None:
        adapter = JsonProcessLeaseAdapter()
        with pytest.raises(NotImplementedError):
            adapter.save(_lease(), expected_revision=Revision(1))

    def test_list_all_raises_not_implemented(self) -> None:
        adapter = JsonProcessLeaseAdapter()
        with pytest.raises(NotImplementedError):
            adapter.list_all()

    def test_delete_raises_not_implemented(self) -> None:
        adapter = JsonProcessLeaseAdapter()
        with pytest.raises(NotImplementedError):
            adapter.delete(_LID, expected_revision=Revision(1))


# --------------------------------------------------------------------------- JsonRuntimeTransitionAdapter


class TestJsonRuntimeTransitionAdapter:
    """Given a JsonRuntimeTransitionAdapter, every method raises NotImplementedError (deferred to Phase 3)."""

    def test_create_raises_not_implemented(self) -> None:
        adapter = JsonRuntimeTransitionAdapter()
        with pytest.raises(NotImplementedError):
            adapter.create(_transition())

    def test_read_raises_not_implemented(self) -> None:
        adapter = JsonRuntimeTransitionAdapter()
        with pytest.raises(NotImplementedError):
            adapter.read(_TID)

    def test_save_raises_not_implemented(self) -> None:
        adapter = JsonRuntimeTransitionAdapter()
        with pytest.raises(NotImplementedError):
            adapter.save(_transition(), expected_revision=Revision(1))

    def test_list_all_raises_not_implemented(self) -> None:
        adapter = JsonRuntimeTransitionAdapter()
        with pytest.raises(NotImplementedError):
            adapter.list_all()

    def test_list_by_generation_raises_not_implemented(self) -> None:
        adapter = JsonRuntimeTransitionAdapter()
        with pytest.raises(NotImplementedError):
            adapter.list_by_generation(1)
