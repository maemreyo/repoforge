"""Shared helpers for execution-worker reconciliation and reporting.

Kept out of ``execution_worker_reconciler.py`` so that file stays under the
400-line policy. Nothing here holds safety policy: the reconciler owns the
fail-closed decisions; this module only provides the registry digest, the
live-concern state sets, and the reader type aliases both files share.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from typing import Protocol

from ...domain.execution_worker import ExecutionWorkerBinding, is_execution_worker_entry_point
from ...domain.process_lease import (
    ACTIVE_LEASE_STATUSES,
    ProcessLease,
    ProcessLeaseStatus,
)


class ProcessIdentityLike(Protocol):
    """The one field the reconciler reads off a process identity: the start token."""

    @property
    def start_token(self) -> str | None: ...


OwnerIdentityReader = Callable[[int], str | None]
CommandLineReader = Callable[[int], tuple[str, ...] | None]
ProcessIdentityReader = Callable[[int], ProcessIdentityLike | None]
ProcessGroupGoneReader = Callable[[int], bool]
NowIso = Callable[[], str]

#: Binding states that remain a live safety concern. ``refused_unproven``,
#: ``survived_kill``, and ``legacy_unproven`` are active concerns: the process may
#: still be running and holding locks, so they are re-evaluated every pass.
ACTIVE_BINDING_STATES = frozenset(
    {"running", "legacy_unproven", "refused_unproven", "survived_kill"}
)

#: Lease statuses that describe a spawn still in flight (pre-spawn intent or an
#: unclaimed pid). These are the durable admission fence members: while one
#: exists, a replacement may not be started and the incumbent may not be stopped.
FENCE_LEASE_STATUSES: frozenset[ProcessLeaseStatus] = frozenset(
    {
        ProcessLeaseStatus.REGISTERED,
        ProcessLeaseStatus.READY,
    }
)

#: Terminal lease statuses, archived out of the active scan by ``collect_terminal``.
TERMINAL_LEASE_STATUSES: frozenset[ProcessLeaseStatus] = frozenset(
    {
        ProcessLeaseStatus.TERMINATED,
        ProcessLeaseStatus.ARCHIVED,
    }
)

#: Reaping outcomes that prove the process and its group are gone. These
#: terminalize the canonical ProcessLease (RUNNING -> TERMINATING -> TERMINATED)
#: and the binding projection, then archive the active lease record.
TERMINAL_OUTCOMES = frozenset({"reclaimed", "already_gone"})

#: Reaping outcomes that leave the worker a live concern. The process may still
#: be alive and holding locks, so the lease moves to KILLED / UNPROVEN and stays
#: active until a later pass proves death.
KEEP_ACTIVE_OUTCOMES = frozenset({"survived_kill", "refused_unproven"})

WORKER_LIFECYCLE_OUTCOMES = TERMINAL_OUTCOMES | KEEP_ACTIVE_OUTCOMES


class WorkerConcern(Protocol):
    """A worker the reconciler can prove and reap: binding or lease concern.

    Structurally satisfied by both ``ExecutionWorkerBinding`` and the binding-less
    ``LeaseConcern`` so every proof and the outcome recorder work unchanged on
    either source. Nothing here holds policy; the reconciler owns the fail-closed
    decisions (P0-1).
    """

    @property
    def worker_id(self) -> str: ...
    @property
    def pid(self) -> int: ...
    @property
    def pgid(self) -> int: ...
    @property
    def process_start_token(self) -> str | None: ...
    @property
    def supervisor_pid(self) -> int: ...
    @property
    def supervisor_process_identity(self) -> str | None: ...
    @property
    def release_sha(self) -> str | None: ...


def still_owned(owner_identity_reader: OwnerIdentityReader, concern: WorkerConcern) -> bool:
    """Is the recorded owner supervisor alive and still the same process?"""
    if concern.supervisor_process_identity is None:
        return False
    return owner_identity_reader(concern.supervisor_pid) == concern.supervisor_process_identity


def proven_execution_worker(
    command_line_reader: CommandLineReader,
    identity_reader: ProcessIdentityReader,
    concern: WorkerConcern,
) -> bool:
    """Prove the process is still the same worker with the exact entry point."""
    if concern.process_start_token is None:
        return False
    argv = command_line_reader(concern.pid)
    if argv is None:
        return False
    if not is_execution_worker_entry_point(argv):
        return False
    identity = identity_reader(concern.pid)
    return not (identity is None or identity.start_token != concern.process_start_token)


def provably_gone(
    identity_reader: ProcessIdentityReader,
    process_group_gone: ProcessGroupGoneReader | None,
    concern: WorkerConcern,
) -> bool:
    """Is the process AND its group proven absent, so no signal is needed?"""
    if identity_reader(concern.pid) is not None:
        return False
    if process_group_gone is None:
        return False
    return process_group_gone(concern.pgid)


def registry_digest(records: tuple[ExecutionWorkerBinding, ...]) -> str:
    entries = sorted(
        (
            f"{binding.worker_id}:{binding.state}:{binding.pid}:{binding.pgid}:"
            f"{binding.process_start_token or ''}:{binding.generation}:"
            f"{binding.release_sha or ''}:{binding.supervisor_process_identity}"
        )
        for binding in records
        if binding.state in ACTIVE_BINDING_STATES
    )
    return hashlib.sha256("\n".join(entries).encode("utf-8")).hexdigest()


def lease_registry_digest(leases: tuple[ProcessLease, ...]) -> str:
    """Digest of the live-concern lease set, for the F-004 handoff fence.

    Includes every active-status lease with its pid/token/owner/release so a pid
    reuse, a token change, or a new spawn all invalidate the plan digest.
    """
    entries = sorted(
        (
            f"{lease.lease_id}:{lease.status.value}:{lease.pid}:{lease.pgid}:"
            f"{lease.process_start_token or ''}:{lease.owner_pid}:"
            f"{lease.owner_process_identity or ''}:{lease.release_sha or ''}:"
            f"{lease.generation}:{lease.admission_epoch}"
        )
        for lease in leases
        if lease.status in ACTIVE_LEASE_STATUSES
    )
    return hashlib.sha256("\n".join(entries).encode("utf-8")).hexdigest()


__all__ = [
    "ACTIVE_BINDING_STATES",
    "ACTIVE_LEASE_STATUSES",
    "FENCE_LEASE_STATUSES",
    "TERMINAL_LEASE_STATUSES",
    "CommandLineReader",
    "NowIso",
    "OwnerIdentityReader",
    "ProcessGroupGoneReader",
    "ProcessIdentityLike",
    "ProcessIdentityReader",
    "WorkerConcern",
    "lease_registry_digest",
    "provably_gone",
    "proven_execution_worker",
    "registry_digest",
    "still_owned",
]
