"""Persistence boundary for immutable, content-addressed evidence artifacts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ..domain.evidence_artifact import (
    EvidenceArtifactKind,
    EvidenceArtifactReference,
    EvidenceRetentionClass,
)


@dataclass(frozen=True, slots=True)
class EvidenceArtifactRange:
    """One bounded window of a stored artifact, with its own continuation."""

    reference: EvidenceArtifactReference
    content: bytes
    byte_offset: int
    next_byte_offset: int | None
    truncated: bool


class EvidenceArtifactStore(Protocol):
    def persist(
        self,
        payload: bytes,
        *,
        kind: EvidenceArtifactKind,
        media_type: str,
        retention_class: EvidenceRetentionClass,
    ) -> EvidenceArtifactReference: ...

    def read_range(
        self,
        reference: str,
        *,
        byte_offset: int = 0,
        max_bytes: int = 120_000,
    ) -> EvidenceArtifactRange: ...
