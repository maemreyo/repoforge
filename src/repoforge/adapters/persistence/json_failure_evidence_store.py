"""Private atomic persistence for normalized execution failure evidence."""

from __future__ import annotations

import re
from pathlib import Path

from ...domain.durable_state import SchemaVersion, StateCodec
from ...domain.errors import ErrorCode, RepoForgeError
from ...domain.failure_intelligence import (
    FAILURE_EVIDENCE_SCHEMA_VERSION,
    FailureEvidence,
    failure_evidence_from_payload,
    failure_evidence_payload,
    validate_failure_evidence,
)
from ...ports.failure_evidence_store import FailureEvidencePage, FailureEvidenceStore
from ...ports.locking import LockManager
from .json_state_repository import JsonStateRepository

_FAILURE_ID = re.compile(r"^failure-[a-f0-9]{24}$")


def _failure_id(value: str) -> str:
    if _FAILURE_ID.fullmatch(value) is None:
        raise ValueError("invalid failure evidence id")
    return value


class _FailureCodec(StateCodec[FailureEvidence]):
    schema_version = SchemaVersion(FAILURE_EVIDENCE_SCHEMA_VERSION)

    def encode(self, value: FailureEvidence) -> dict[str, object]:
        validate_failure_evidence(value)
        return failure_evidence_payload(value)

    def decode(self, payload: dict[str, object]) -> FailureEvidence:
        return failure_evidence_from_payload(dict(payload))


class JsonFailureEvidenceStore(FailureEvidenceStore):
    def __init__(self, state_root: Path, locks: LockManager) -> None:
        self._records = JsonStateRepository(
            state_root,
            collection="failure-evidence",
            locks=locks,
            codec=_FailureCodec(),
            id_validator=_failure_id,
            max_record_bytes=256_000,
        )
        self.root = self._records.root

    @staticmethod
    def _translate(exc: RepoForgeError) -> RepoForgeError:
        if exc.code is ErrorCode.STATE_SCHEMA_UNSUPPORTED:
            return RepoForgeError(
                "Failure evidence schema is unsupported",
                code=ErrorCode.EVIDENCE_SCHEMA_UNSUPPORTED,
            )
        if exc.code in {
            ErrorCode.STATE_CORRUPT,
            ErrorCode.STATE_INVALID,
            ErrorCode.STATE_TOO_LARGE,
        }:
            return RepoForgeError(
                "Failure evidence record is corrupt",
                code=ErrorCode.EVIDENCE_CORRUPT,
            )
        return exc

    def create(self, evidence: FailureEvidence) -> FailureEvidence:
        validate_failure_evidence(evidence)
        try:
            existing = self._records.read(evidence.failure_id)
            if existing is not None:
                if existing.value != evidence:
                    raise RepoForgeError(
                        "Failure evidence identity is bound to different content",
                        code=ErrorCode.EVIDENCE_CORRUPT,
                    )
                return existing.value
            return self._records.create(evidence.failure_id, evidence).value
        except RepoForgeError as exc:
            raise self._translate(exc) from exc

    def read(self, failure_id: str) -> FailureEvidence | None:
        try:
            envelope = self._records.read(failure_id)
        except RepoForgeError as exc:
            raise self._translate(exc) from exc
        return envelope.value if envelope is not None else None

    def _page(self, predicate: object, *, max_records: int) -> FailureEvidencePage:
        try:
            page = self._records.list_records(max_records=max_records)
        except RepoForgeError as exc:
            raise self._translate(exc) from exc
        records = [
            item.value for item in page.records if callable(predicate) and predicate(item.value)
        ]
        records.sort(key=lambda item: (item.created_at, item.failure_id), reverse=True)
        return FailureEvidencePage(tuple(records), page.scan_truncated)

    def list_for_operation(
        self, operation_id: str, *, max_records: int = 500
    ) -> FailureEvidencePage:
        return self._page(
            lambda evidence: evidence.operation_id == operation_id,
            max_records=max_records,
        )

    def list_for_binding(self, binding_hash: str, *, max_records: int = 500) -> FailureEvidencePage:
        return self._page(
            lambda evidence: evidence.compatibility_binding == binding_hash,
            max_records=max_records,
        )
