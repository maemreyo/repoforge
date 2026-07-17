"""Reusable typed envelopes for bounded durable application state."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, Protocol, TypeVar

from .versioning import Revision as Revision
from .versioning import SchemaVersion as SchemaVersion

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class StateEnvelope(Generic[T]):
    record_id: str
    schema_version: SchemaVersion
    revision: Revision
    value: T


@dataclass(frozen=True, slots=True)
class StatePage(Generic[T]):
    records: tuple[StateEnvelope[T], ...]
    scan_truncated: bool


class StateCodec(Protocol[T]):
    schema_version: SchemaVersion

    def encode(self, value: T) -> dict[str, object]: ...

    def decode(self, payload: dict[str, object]) -> T: ...


def state_audit_metadata(envelope: StateEnvelope[object], *, action: str) -> dict[str, object]:
    """Return the only durable-state fields safe for generic audit metadata."""
    return {
        "action": action,
        "record_id": envelope.record_id,
        "revision": envelope.revision.value,
        "schema_version": envelope.schema_version.value,
    }
