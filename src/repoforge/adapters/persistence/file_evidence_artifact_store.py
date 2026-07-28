"""Content-addressed, byte-preserving storage for evidence artifacts.

Deliberately separate from the failure-output store, which redacts before it
writes. Both are content-addressed and immutable; they differ on the one thing
that matters to a caller about to reuse the content. Here the bytes are kept
exactly as given for the kinds that may be written back into a repository, and
the reference says so. Command output still goes through redaction, because it
routinely carries credentials from the environment it ran in and nothing
legitimately writes it back.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from ...domain.errors import ErrorCode, RepoForgeError
from ...domain.evidence_artifact import (
    MAX_EVIDENCE_ARTIFACT_BYTES,
    EvidenceArtifactKind,
    EvidenceArtifactReference,
    EvidenceRedactionStatus,
    EvidenceRetentionClass,
    artifact_digest,
    new_artifact_reference,
    parse_artifact_reference,
)
from ...domain.redaction import redact_text
from ...ports.evidence_artifact_store import EvidenceArtifactRange

_DIRECTORY = "evidence-artifacts"
_MAX_RANGE_BYTES = 1_000_000
_RAW_ELIGIBLE = frozenset(
    {
        EvidenceArtifactKind.SOURCE,
        EvidenceArtifactKind.PATCH,
        EvidenceArtifactKind.CONFLICT_BODY,
    }
)


def _error(message: str, code: ErrorCode = ErrorCode.STATE_INVALID) -> RepoForgeError:
    return RepoForgeError(message, code=code)


class FileEvidenceArtifactStore:
    def __init__(self, state_root: Path) -> None:
        self._root = state_root / _DIRECTORY

    def _path(self, kind: EvidenceArtifactKind, digest: str) -> Path:
        return self._root / f"{kind.value}-{digest}.blob"

    def persist(
        self,
        payload: bytes,
        *,
        kind: EvidenceArtifactKind,
        media_type: str,
        retention_class: EvidenceRetentionClass = EvidenceRetentionClass.OPERATION,
    ) -> EvidenceArtifactReference:
        """Store one artifact and describe it, without ever rewriting an existing one."""

        if not isinstance(payload, bytes):
            raise _error("evidence artifact payload must be bytes")
        if len(payload) > MAX_EVIDENCE_ARTIFACT_BYTES:
            raise _error("evidence artifact payload exceeds the retrieval bound")
        stored = payload
        redaction_status = EvidenceRedactionStatus.RAW
        if kind not in _RAW_ELIGIBLE:
            stored = redact_text(
                payload.decode("utf-8", errors="replace"),
                limit=MAX_EVIDENCE_ARTIFACT_BYTES,
            ).encode("utf-8", errors="replace")
            redaction_status = EvidenceRedactionStatus.REDACTED
        descriptor = new_artifact_reference(
            kind=kind,
            payload=stored,
            media_type=media_type,
            redaction_status=redaction_status,
            retention_class=retention_class,
        )
        target = self._path(kind, descriptor.digest)
        try:
            self._root.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(self._root, 0o700)
            if target.is_symlink():
                raise _error("evidence artifact path is a symlink")
            if target.exists():
                # Content-addressed, so an existing file is the same content by
                # construction -- unless it is not, in which case the store has
                # been tampered with and must not be trusted to serve it.
                existing = target.read_bytes()
                if artifact_digest(existing) != descriptor.digest:
                    raise _error(
                        "evidence artifact on disk does not match its digest",
                        ErrorCode.STATE_CORRUPT,
                    )
                return descriptor
            handle_fd, temporary_name = tempfile.mkstemp(
                prefix=f".{descriptor.digest}.tmp-", dir=self._root
            )
            temporary = Path(temporary_name)
            try:
                with os.fdopen(handle_fd, "wb") as handle:
                    os.fchmod(handle.fileno(), 0o600)
                    handle.write(stored)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, target)
                os.chmod(target, 0o600)
            finally:
                temporary.unlink(missing_ok=True)
        except OSError as exc:
            raise _error("evidence artifact could not be persisted") from exc
        return descriptor

    def read_range(
        self,
        reference: str,
        *,
        byte_offset: int = 0,
        max_bytes: int = 120_000,
    ) -> EvidenceArtifactRange:
        """Return one bounded window, digest-verified against the whole artifact."""

        kind, digest = parse_artifact_reference(reference)
        if not isinstance(byte_offset, int) or isinstance(byte_offset, bool) or byte_offset < 0:
            raise _error("evidence artifact byte_offset must be a non-negative integer")
        if (
            not isinstance(max_bytes, int)
            or isinstance(max_bytes, bool)
            or not 1 <= max_bytes <= _MAX_RANGE_BYTES
        ):
            raise _error(f"evidence artifact max_bytes must be between 1 and {_MAX_RANGE_BYTES}")
        target = self._path(kind, digest)
        try:
            payload = target.read_bytes()
        except FileNotFoundError as exc:
            raise _error("evidence artifact was not found", ErrorCode.STATE_NOT_FOUND) from exc
        except OSError as exc:
            raise _error("evidence artifact is unreadable") from exc
        # Verified on every read, not only at write: the reference is the caller's
        # only guarantee that the window it is about to use came from the content
        # it asked for.
        if artifact_digest(payload) != digest:
            raise _error(
                "evidence artifact digest does not match its reference",
                ErrorCode.STATE_CORRUPT,
            )
        if byte_offset > len(payload):
            raise _error("evidence artifact byte_offset is outside the artifact")
        descriptor = new_artifact_reference(
            kind=kind,
            payload=payload,
            media_type="application/octet-stream",
            redaction_status=(
                EvidenceRedactionStatus.RAW
                if kind in _RAW_ELIGIBLE
                else EvidenceRedactionStatus.REDACTED
            ),
            retention_class=EvidenceRetentionClass.OPERATION,
        )
        window = payload[byte_offset : byte_offset + max_bytes]
        consumed = byte_offset + len(window)
        has_more = consumed < len(payload)
        return EvidenceArtifactRange(
            reference=descriptor,
            content=window,
            byte_offset=byte_offset,
            next_byte_offset=consumed if has_more else None,
            truncated=has_more,
        )
