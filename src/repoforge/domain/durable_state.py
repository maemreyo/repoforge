"""Reusable typed envelopes for bounded durable application state."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, Protocol, TypeVar

T = TypeVar("T")


@dataclass(frozen=True, slots=True, order=True)
class SchemaVersion:
    value: int

    def __post_init__(self) -> None:
        if not isinstance(self.value, int) or isinstance(self.value, bool) or self.value <= 0:
            raise ValueError("schema version must be a positive integer")


@dataclass(frozen=True, slots=True, order=True)
class Revision:
    value: int

    def __post_init__(self) -> None:
        if not isinstance(self.value, int) or isinstance(self.value, bool) or self.value <= 0:
            raise ValueError("revision must be a positive integer")

    def next(self) -> Revision:
        return Revision(self.value + 1)


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
