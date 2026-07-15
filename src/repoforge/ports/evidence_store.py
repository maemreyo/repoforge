"""Persistence boundary for provider-neutral normalized evidence."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ..domain.evidence import EvidenceItem, EvidenceQuery, EvidenceSnapshot


@dataclass(frozen=True, slots=True)
class EvidencePage:
    items: tuple[EvidenceItem, ...]
    next_cursor: str | None
    scan_truncated: bool = False


@dataclass(frozen=True, slots=True)
class EvidenceRetentionReport:
    deleted_for_age: int
    deleted_for_count: int
    deleted_for_bytes: int
    deleted_artifacts: int
    protected_items: int
    remaining_items: int
    total_bytes: int


class EvidenceStore(Protocol):
    def create(self, item: EvidenceItem, *, artifact: bytes | None = None) -> EvidenceItem: ...

    def read(self, evidence_id: str) -> EvidenceItem | None: ...

    def read_artifact(self, digest: str) -> bytes: ...

    def query(
        self,
        query: EvidenceQuery,
        *,
        current_snapshot: EvidenceSnapshot | None = None,
        now: str | None = None,
    ) -> EvidencePage: ...

    def prune(
        self,
        *,
        now: str,
        retention_seconds: int,
        max_items: int,
        max_total_bytes: int,
        protected_evidence_ids: tuple[str, ...] = (),
    ) -> EvidenceRetentionReport: ...
