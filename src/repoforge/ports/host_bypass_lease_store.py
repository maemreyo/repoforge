"""Durable store for operator-issued `trusted_host` capability leases (#383)."""

from __future__ import annotations

from typing import Protocol

from ..domain.durable_state import Revision, StateEnvelope, StatePage
from ..domain.host_bypass_lease import HostBypassLease


class HostBypassLeaseStore(Protocol):
    def create(self, lease: HostBypassLease) -> StateEnvelope[HostBypassLease]: ...

    def read(self, lease_id: str) -> StateEnvelope[HostBypassLease] | None: ...

    def save(
        self, lease: HostBypassLease, *, expected_revision: Revision
    ) -> StateEnvelope[HostBypassLease]: ...

    def list_records(self, *, max_records: int = 500) -> StatePage[HostBypassLease]: ...
