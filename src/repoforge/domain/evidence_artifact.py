"""Immutable, content-addressed evidence artifacts and their bounded references.

A result that cannot carry its whole evidence inline carries a reference instead.
The reference is small, typed and self-describing: a caller can tell how large
the artifact is, what it contains, and -- decisively -- whether what it will read
back is the exact bytes or a redacted rendering.

That last distinction is the reason this exists alongside the failure-output
store, which redacts before it writes. Redacting at write time is right for
command output, and wrong for source: an agent that reads a file, edits it and
writes it back must receive the exact bytes, because a display-redaction token
substituted into mutation input silently corrupts the file it claims to fix.
So the artifact records which of the two it is, and a caller never has to guess.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from enum import Enum

from .errors import ErrorCode, RepoForgeError

EVIDENCE_ARTIFACT_SCHEMA_VERSION = 1

_DIGEST = re.compile(r"^[a-f0-9]{64}$")
_REFERENCE = re.compile(r"^evidence:(?P<kind>[a-z][a-z0-9_-]{0,31}):(?P<digest>[a-f0-9]{64})$")
_MEDIA_TYPE = re.compile(r"^[a-z]+/[a-z0-9.+-]{1,96}$")

#: Ten megabytes, matching the existing failure-output artifact bound. A single
#: piece of evidence larger than this is a sign the caller should have narrowed
#: its request, not a reason to grow the store.
MAX_EVIDENCE_ARTIFACT_BYTES = 10 * 1024 * 1024


class EvidenceArtifactKind(str, Enum):
    """What the artifact holds, which decides how it may be used."""

    SOURCE = "source"
    """Exact file content. Byte-preserving; safe to round-trip into a mutation."""

    PATCH = "patch"
    """A unified diff. Byte-preserving; applied against an exact digest."""

    CONFLICT_BODY = "conflict_body"
    """One merge-conflict body. Byte-preserving."""

    COMMAND_OUTPUT = "command_output"
    """Captured stdout/stderr. Redacted before storage; never mutation input."""


class EvidenceRedactionStatus(str, Enum):
    RAW = "raw"
    """Stored byte-for-byte. Reading it back reproduces the source exactly."""

    REDACTED = "redacted"
    """Secret-shaped ranges were removed before storage. Not byte-exact."""


class EvidenceRetentionClass(str, Enum):
    OPERATION = "operation"
    """Retained while its operation is retained, then collectable."""

    SESSION = "session"
    """Retained for the working session; collectable once nothing references it."""


#: Which kinds may be stored raw. Command output is deliberately excluded: it is
#: the one kind routinely carrying credentials from the environment it ran in,
#: and nothing legitimately writes it back into the repository.
_RAW_ELIGIBLE_KINDS = frozenset(
    {
        EvidenceArtifactKind.SOURCE,
        EvidenceArtifactKind.PATCH,
        EvidenceArtifactKind.CONFLICT_BODY,
    }
)


def _error(message: str) -> RepoForgeError:
    return RepoForgeError(message, code=ErrorCode.STATE_INVALID)


@dataclass(frozen=True, slots=True)
class EvidenceArtifactReference:
    """The bounded, public description of one stored artifact."""

    reference: str
    kind: EvidenceArtifactKind
    digest: str
    byte_count: int
    media_type: str
    redaction_status: EvidenceRedactionStatus
    retention_class: EvidenceRetentionClass
    schema_version: int = EVIDENCE_ARTIFACT_SCHEMA_VERSION

    @property
    def byte_exact(self) -> bool:
        """Whether reading this artifact reproduces the original bytes.

        The only property a caller should consult before using retrieved content
        as mutation input.
        """
        return self.redaction_status is EvidenceRedactionStatus.RAW


def artifact_digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def artifact_reference(kind: EvidenceArtifactKind, digest: str) -> str:
    if not isinstance(kind, EvidenceArtifactKind):
        raise _error("evidence artifact kind is invalid")
    if not isinstance(digest, str) or _DIGEST.fullmatch(digest) is None:
        raise _error("evidence artifact digest must be a lowercase sha256")
    return f"evidence:{kind.value}:{digest}"


def parse_artifact_reference(reference: str) -> tuple[EvidenceArtifactKind, str]:
    """Split a reference back into its kind and digest, refusing anything else."""

    if not isinstance(reference, str):
        raise _error("evidence artifact reference must be a string")
    match = _REFERENCE.fullmatch(reference)
    if match is None:
        raise _error("evidence artifact reference is invalid")
    try:
        kind = EvidenceArtifactKind(match.group("kind"))
    except ValueError as exc:
        raise _error("evidence artifact reference names an unknown kind") from exc
    return kind, match.group("digest")


def new_artifact_reference(
    *,
    kind: EvidenceArtifactKind,
    payload: bytes,
    media_type: str,
    redaction_status: EvidenceRedactionStatus,
    retention_class: EvidenceRetentionClass,
) -> EvidenceArtifactReference:
    """Describe one artifact, refusing a combination that cannot be honoured."""

    if not isinstance(payload, bytes):
        raise _error("evidence artifact payload must be bytes")
    if not payload:
        raise _error("evidence artifact payload must not be empty")
    if len(payload) > MAX_EVIDENCE_ARTIFACT_BYTES:
        raise _error("evidence artifact payload exceeds the retrieval bound")
    if not isinstance(media_type, str) or _MEDIA_TYPE.fullmatch(media_type) is None:
        raise _error("evidence artifact media type is invalid")
    if not isinstance(redaction_status, EvidenceRedactionStatus):
        raise _error("evidence artifact redaction status is invalid")
    if not isinstance(retention_class, EvidenceRetentionClass):
        raise _error("evidence artifact retention class is invalid")
    if redaction_status is EvidenceRedactionStatus.RAW and kind not in _RAW_ELIGIBLE_KINDS:
        # Refused rather than silently downgraded: a caller that asked for raw
        # command output has misunderstood what it is about to read back, and a
        # quiet redaction would hand it non-exact bytes under an exact name.
        raise _error(f"evidence artifacts of kind {kind.value!r} may not be stored raw")
    digest = artifact_digest(payload)
    return EvidenceArtifactReference(
        reference=artifact_reference(kind, digest),
        kind=kind,
        digest=digest,
        byte_count=len(payload),
        media_type=media_type,
        redaction_status=redaction_status,
        retention_class=retention_class,
    )
