"""Persistence boundary for normalized execution failure evidence."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ..domain.failure_intelligence import FailureEvidence


@dataclass(frozen=True, slots=True)
class FailureEvidencePage:
    records: tuple[FailureEvidence, ...]
    scan_truncated: bool


class FailureEvidenceStore(Protocol):
    def create(self, evidence: FailureEvidence) -> FailureEvidence: ...

    def read(self, failure_id: str) -> FailureEvidence | None: ...

    def list_for_operation(
        self, operation_id: str, *, max_records: int = 500
    ) -> FailureEvidencePage: ...

    def list_for_binding(
        self, binding_hash: str, *, max_records: int = 500
    ) -> FailureEvidencePage: ...
