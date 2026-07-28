"""Durable operation identity sidecar boundary."""

from __future__ import annotations

from typing import Protocol

from ..domain.durable_state import Revision, StateEnvelope, StatePage
from ..domain.operation_identity import OperationIdentityRecord


class OperationIdentityStore(Protocol):
    def create(self, record: OperationIdentityRecord) -> StateEnvelope[OperationIdentityRecord]: ...

    def read(self, operation_id: str) -> StateEnvelope[OperationIdentityRecord] | None: ...

    def save(
        self,
        record: OperationIdentityRecord,
        *,
        expected_revision: Revision,
    ) -> StateEnvelope[OperationIdentityRecord]: ...

    def list_records(self, *, max_records: int) -> StatePage[OperationIdentityRecord]: ...

    def delete(self, operation_id: str) -> None: ...
