"""Tests for ``ProcessLease`` — typed process-lease state machine with module-level transition functions."""

from __future__ import annotations

import pytest

from repoforge.domain.process_lease import (
    LeaseId,
    ProcessLease,
    ProcessLeaseStatus,
    archive,
    begin_termination,
    confirm_terminated,
    mark_unproven,
    quarantine,
    register_ready,
    survive_kill,
)

_REGISTERED_KWARGS = dict(
    lease_id="lease-000000000000000000000001",
    status=ProcessLeaseStatus.REGISTERED,
    process_identity=None,
    pid=None,
    started_at=None,
    heartbeat_at=None,
    correlation_id="corr-001",
    created_at="2026-01-01T00:00:00+00:00",
    updated_at="2026-01-01T00:00:00+00:00",
)


def _lease(**overrides: str | None | int | ProcessLeaseStatus) -> ProcessLease:
    """Build a minimal ProcessLease with overridable fields."""
    kwargs = dict(_REGISTERED_KWARGS)
    kwargs.update(overrides)  # type: ignore[typeddict-item]
    return ProcessLease(**kwargs)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- construction & defaults


class TestConstruction:
    """Given a set of valid fields, when constructing a ProcessLease, then it is created correctly."""

    def test_defaults_error_fields_to_none(self) -> None:
        """Given no error_code or error_message, when constructing, then they are None."""
        lease = _lease()
        assert lease.error_code is None
        assert lease.error_message is None

    def test_lease_id_typed_accessor(self) -> None:
        """Given a lease, when accessing lease_id_typed, then it returns a LeaseId."""
        lease = _lease()
        typed = lease.lease_id_typed
        assert isinstance(typed, str)
        assert typed == lease.lease_id

    def test_accepts_none_pid(self) -> None:
        """Given pid=None, when constructing, then pid is None (registered state)."""
        lease = _lease(pid=None)
        assert lease.pid is None


# --------------------------------------------------------------------------- field validation


class TestFieldValidation:
    """Given invalid field values, when constructing, then ValueError is raised."""

    def test_rejects_empty_lease_id(self) -> None:
        with pytest.raises(ValueError, match="lease_id"):
            _lease(lease_id="")

    def test_rejects_whitespace_lease_id(self) -> None:
        with pytest.raises(ValueError, match="lease_id"):
            _lease(lease_id="  ")

    def test_rejects_empty_correlation_id(self) -> None:
        with pytest.raises(ValueError, match="correlation_id"):
            _lease(correlation_id="")

    def test_rejects_empty_created_at(self) -> None:
        with pytest.raises(ValueError, match="created_at"):
            _lease(created_at="")

    def test_rejects_empty_updated_at(self) -> None:
        with pytest.raises(ValueError, match="updated_at"):
            _lease(updated_at="")

    def test_rejects_zero_pid(self) -> None:
        with pytest.raises(ValueError, match="pid"):
            _lease(pid=0)

    def test_rejects_negative_pid(self) -> None:
        with pytest.raises(ValueError, match="pid"):
            _lease(pid=-1)

    def test_rejects_empty_started_at(self) -> None:
        with pytest.raises(ValueError, match="started_at"):
            _lease(status=ProcessLeaseStatus.RUNNING, started_at="")

    def test_rejects_empty_heartbeat_at(self) -> None:
        with pytest.raises(ValueError, match="heartbeat_at"):
            _lease(heartbeat_at="")

    def test_rejects_empty_process_identity(self) -> None:
        with pytest.raises(ValueError, match="process_identity"):
            _lease(process_identity="")


# --------------------------------------------------------------------------- happy path transitions


class TestHappyPathTransitions:
    """Given a ProcessLease, when calling valid transition functions, then each produces the expected status."""

    def test_register_ready_moves_from_registered_to_ready(self) -> None:
        """Given a REGISTERED lease, when register_ready is called, then it becomes READY."""
        lease = _lease()
        ready = register_ready(
            lease,
            updated_at="2026-01-01T00:00:01+00:00",
            process_identity="tok-abc",
            pid=1001,
        )
        assert ready.status is ProcessLeaseStatus.READY
        assert ready.pid == 1001
        assert ready.process_identity == "tok-abc"

    def test_register_ready_preserves_error_fields(self) -> None:
        """Given a REGISTERED lease, when register_ready is called, error fields remain None."""
        lease = _lease()
        ready = register_ready(
            lease,
            updated_at="2026-01-01T00:00:01+00:00",
            process_identity="tok-abc",
            pid=1001,
        )
        assert ready.error_code is None
        assert ready.error_message is None

    def test_mark_unproven_moves_from_ready_to_unproven(self) -> None:
        """Given a READY lease, when mark_unproven is called, then it becomes UNPROVEN."""
        lease = _lease(status=ProcessLeaseStatus.READY, pid=1001, process_identity="tok-a")
        unproven = mark_unproven(
            lease,
            updated_at="2026-01-01T00:00:01+00:00",
            error_code="UNPROVEN",
            error_message="identity mismatch",
        )
        assert unproven.status is ProcessLeaseStatus.UNPROVEN
        assert unproven.error_code == "UNPROVEN"
        assert unproven.error_message == "identity mismatch"

    def test_begin_termination_moves_from_running_to_terminating(self) -> None:
        """Given a RUNNING lease, when begin_termination is called, then it becomes TERMINATING."""
        lease = _lease(
            status=ProcessLeaseStatus.RUNNING,
            pid=1001,
            process_identity="tok-a",
        )
        terminating = begin_termination(lease, updated_at="2026-01-01T00:00:02+00:00")
        assert terminating.status is ProcessLeaseStatus.TERMINATING

    def test_confirm_terminated_from_terminating(self) -> None:
        """Given a TERMINATING lease, when confirm_terminated is called, then it becomes TERMINATED."""
        lease = _lease(status=ProcessLeaseStatus.TERMINATING)
        terminated = confirm_terminated(lease, updated_at="2026-01-01T00:00:03+00:00")
        assert terminated.status is ProcessLeaseStatus.TERMINATED

    def test_confirm_terminated_from_killed(self) -> None:
        """Given a KILLED lease, when confirm_terminated is called, then it becomes TERMINATED."""
        lease = _lease(status=ProcessLeaseStatus.KILLED)
        terminated = confirm_terminated(lease, updated_at="2026-01-01T00:00:03+00:00")
        assert terminated.status is ProcessLeaseStatus.TERMINATED

    def test_confirm_terminated_from_quarantined(self) -> None:
        """Given a QUARANTINED lease, when confirm_terminated is called, then it becomes TERMINATED."""
        lease = _lease(status=ProcessLeaseStatus.QUARANTINED)
        terminated = confirm_terminated(lease, updated_at="2026-01-01T00:00:03+00:00")
        assert terminated.status is ProcessLeaseStatus.TERMINATED

    def test_archive_moves_from_terminated_to_archived(self) -> None:
        """Given a TERMINATED lease, when archive is called, then it becomes ARCHIVED."""
        lease = _lease(status=ProcessLeaseStatus.TERMINATED)
        archived = archive(lease, updated_at="2026-01-01T00:00:04+00:00")
        assert archived.status is ProcessLeaseStatus.ARCHIVED

    def test_survive_kill_moves_from_running_to_killed(self) -> None:
        """Given a RUNNING lease, when survive_kill is called, then it becomes KILLED."""
        lease = _lease(status=ProcessLeaseStatus.RUNNING)
        killed = survive_kill(lease, updated_at="2026-01-01T00:00:05+00:00")
        assert killed.status is ProcessLeaseStatus.KILLED

    def test_survive_kill_moves_from_terminating_to_killed(self) -> None:
        """Given a TERMINATING lease, when survive_kill is called, then it becomes KILLED."""
        lease = _lease(status=ProcessLeaseStatus.TERMINATING)
        killed = survive_kill(lease, updated_at="2026-01-01T00:00:05+00:00")
        assert killed.status is ProcessLeaseStatus.KILLED

    def test_quarantine_moves_from_running_to_quarantined(self) -> None:
        """Given a RUNNING lease, when quarantine is called, then it becomes QUARANTINED."""
        lease = _lease(status=ProcessLeaseStatus.RUNNING)
        q = quarantine(
            lease,
            updated_at="2026-01-01T00:00:06+00:00",
            reason_code="OPERATOR",
            reason_message="operator removal",
        )
        assert q.status is ProcessLeaseStatus.QUARANTINED
        assert q.error_code == "OPERATOR"


# --------------------------------------------------------------------------- invalid transitions


class TestInvalidTransitions:
    """Given a ProcessLease in a specific state, when an invalid transition is attempted, then ValueError is raised."""

    def test_register_ready_on_ready_raises(self) -> None:
        """Given a READY lease, when register_ready is called, then ValueError is raised."""
        lease = _lease(
            status=ProcessLeaseStatus.READY,
            pid=1001,
            process_identity="tok-a",
        )
        with pytest.raises(ValueError, match="Invalid process lease transition"):
            register_ready(
                lease,
                updated_at="2026-01-01T00:00:01+00:00",
                process_identity="tok-b",
                pid=1002,
            )

    def test_register_ready_on_terminated_raises(self) -> None:
        """Given a TERMINATED lease, when register_ready is called, then ValueError is raised."""
        lease = _lease(status=ProcessLeaseStatus.TERMINATED)
        with pytest.raises(ValueError, match="Invalid process lease transition"):
            register_ready(
                lease,
                updated_at="now",
                process_identity="tok-x",
                pid=9999,
            )

    def test_mark_unproven_on_registered_raises(self) -> None:
        """Given a REGISTERED lease, when mark_unproven is called, then ValueError is raised."""
        lease = _lease()
        with pytest.raises(ValueError, match="Invalid process lease transition"):
            mark_unproven(
                lease,
                updated_at="now",
                error_code="BAD",
                error_message="not ready yet",
            )

    def test_begin_termination_on_ready_raises(self) -> None:
        """Given a READY lease, when begin_termination is called, then ValueError is raised."""
        lease = _lease(status=ProcessLeaseStatus.READY, pid=1001, process_identity="tok-a")
        with pytest.raises(ValueError, match="Invalid process lease transition"):
            begin_termination(lease, updated_at="now")

    def test_archive_on_running_raises(self) -> None:
        """Given a RUNNING lease, when archive is called, then ValueError is raised."""
        lease = _lease(status=ProcessLeaseStatus.RUNNING)
        with pytest.raises(ValueError, match="Invalid process lease transition"):
            archive(lease, updated_at="now")

    def test_confirm_terminated_on_running_raises(self) -> None:
        """Given a RUNNING lease, when confirm_terminated is called, then ValueError is raised."""
        lease = _lease(status=ProcessLeaseStatus.RUNNING)
        with pytest.raises(ValueError, match="Invalid process lease transition"):
            confirm_terminated(lease, updated_at="now")

    def test_quarantine_on_ready_raises(self) -> None:
        """Given a READY lease, when quarantine is called, then ValueError is raised."""
        lease = _lease(status=ProcessLeaseStatus.READY, pid=1001, process_identity="tok-a")
        with pytest.raises(ValueError, match="Invalid process lease transition"):
            quarantine(
                lease,
                updated_at="now",
                reason_code="X",
                reason_message="nope",
            )

    def test_survive_kill_on_ready_raises(self) -> None:
        """Given a READY lease, when survive_kill is called, then ValueError is raised."""
        lease = _lease(status=ProcessLeaseStatus.READY, pid=1001, process_identity="tok-a")
        with pytest.raises(ValueError, match="Invalid process lease transition"):
            survive_kill(lease, updated_at="now")

    def test_archived_is_terminal(self) -> None:
        """Given an ARCHIVED lease, any transition raises ValueError."""
        lease = _lease(status=ProcessLeaseStatus.ARCHIVED)
        with pytest.raises(ValueError, match="Invalid process lease transition"):
            confirm_terminated(lease, updated_at="now")


# --------------------------------------------------------------------------- immutability


class TestImmutability:
    """Given a ProcessLease (frozen dataclass), when trying to set an attribute, then it is rejected."""

    def test_cannot_set_attribute(self) -> None:
        lease = _lease()
        with pytest.raises(AttributeError):
            lease.status = ProcessLeaseStatus.READY  # type: ignore[misc]

    def test_cannot_set_pid(self) -> None:
        lease = _lease()
        with pytest.raises(AttributeError):
            lease.pid = 999  # type: ignore[misc]


# --------------------------------------------------------------------------- LeaseId NewType


class TestLeaseId:
    """Given LeaseId NewType, it behaves as a str at runtime."""

    def test_lease_id_is_string(self) -> None:
        lid = LeaseId("lease-abc")
        assert isinstance(lid, str)

    def test_lease_id_typed_property(self) -> None:
        lease = _lease()
        assert isinstance(lease.lease_id_typed, str)
        assert lease.lease_id_typed == lease.lease_id
