"""Approval metadata and private payload persistence boundaries."""

from __future__ import annotations

from typing import Protocol

from ..domain.approval import ApprovalRequest
from ..domain.durable_state import Revision, StateEnvelope, StatePage


class ApprovalStore(Protocol):
    def create(self, request: ApprovalRequest) -> StateEnvelope[ApprovalRequest]: ...

    def read(self, request_id: str) -> StateEnvelope[ApprovalRequest] | None: ...

    def save(
        self, request: ApprovalRequest, *, expected_revision: Revision
    ) -> StateEnvelope[ApprovalRequest]: ...

    def list_records(self, *, max_records: int) -> StatePage[ApprovalRequest]: ...


class ApprovalPayloadStore(Protocol):
    def digest(self, payload: dict[str, object]) -> str: ...

    def save(self, request_id: str, payload: dict[str, object]) -> str: ...

    def read(self, request_id: str) -> dict[str, object] | None: ...

    def delete(self, request_id: str) -> None: ...
