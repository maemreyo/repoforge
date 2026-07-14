"""Generic durable-state persistence boundary."""

from __future__ import annotations

from typing import Protocol, TypeVar

from ..domain.durable_state import Revision, StateEnvelope, StatePage

T = TypeVar("T")


class StateRepository(Protocol[T]):
    def create(self, record_id: str, value: T) -> StateEnvelope[T]: ...

    def read(self, record_id: str) -> StateEnvelope[T] | None: ...

    def save(
        self,
        record_id: str,
        value: T,
        *,
        expected_revision: Revision,
    ) -> StateEnvelope[T]: ...

    def list_records(self, *, max_records: int) -> StatePage[T]: ...

    def delete(self, record_id: str) -> None: ...
