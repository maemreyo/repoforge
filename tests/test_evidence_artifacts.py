"""Immutable evidence artifacts: byte-exactness is the property that matters."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from repoforge.adapters.persistence.file_evidence_artifact_store import (
    FileEvidenceArtifactStore,
)
from repoforge.domain.errors import ErrorCode, RepoForgeError
from repoforge.domain.evidence_artifact import (
    EvidenceArtifactKind,
    EvidenceRedactionStatus,
    EvidenceRetentionClass,
    parse_artifact_reference,
)

SOURCE = b"def add(a, b):\n    return a + b  # trailing\ttabs and \xc3\xa9 unicode\n"


def test_source_round_trips_byte_for_byte(tmp_path: Path) -> None:
    """The whole point: content read back is safe to write into a mutation.

    A display-redaction token substituted into mutation input silently corrupts
    the file it claims to fix, so an artifact a caller may write back has to
    reproduce the original bytes exactly.
    """
    store = FileEvidenceArtifactStore(tmp_path)

    reference = store.persist(SOURCE, kind=EvidenceArtifactKind.SOURCE, media_type="text/x-python")
    window = store.read_range(reference.reference, max_bytes=1_000_000)

    assert reference.byte_exact is True
    assert reference.redaction_status is EvidenceRedactionStatus.RAW
    assert reference.byte_count == len(SOURCE)
    assert window.content == SOURCE
    assert window.truncated is False
    assert window.next_byte_offset is None


def test_command_output_is_redacted_and_says_so(tmp_path: Path) -> None:
    """Command output carries credentials from the environment it ran in."""
    store = FileEvidenceArtifactStore(tmp_path)
    token = "ghp_A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"

    reference = store.persist(
        f"deploying with api_key={token}\n".encode(),
        kind=EvidenceArtifactKind.COMMAND_OUTPUT,
        media_type="text/plain",
    )
    window = store.read_range(reference.reference)

    assert reference.byte_exact is False
    assert reference.redaction_status is EvidenceRedactionStatus.REDACTED
    assert token.encode() not in window.content
    assert b"deploying with" in window.content


def test_ranges_are_bounded_and_continue_exactly(tmp_path: Path) -> None:
    store = FileEvidenceArtifactStore(tmp_path)
    reference = store.persist(SOURCE, kind=EvidenceArtifactKind.PATCH, media_type="text/x-diff")

    first = store.read_range(reference.reference, max_bytes=10)
    assert first.content == SOURCE[:10]
    assert first.truncated is True
    assert first.next_byte_offset == 10

    rest = store.read_range(reference.reference, byte_offset=first.next_byte_offset)
    assert first.content + rest.content == SOURCE
    assert rest.truncated is False


def test_storage_is_private_immutable_and_digest_verified_on_read(tmp_path: Path) -> None:
    store = FileEvidenceArtifactStore(tmp_path)
    reference = store.persist(SOURCE, kind=EvidenceArtifactKind.SOURCE, media_type="text/x-python")
    _kind, digest = parse_artifact_reference(reference.reference)
    path = tmp_path / "evidence-artifacts" / f"source-{digest}.blob"

    assert os.stat(path.parent).st_mode & 0o777 == 0o700
    assert os.stat(path).st_mode & 0o777 == 0o600
    # Persisting the same content again is a no-op that returns the same identity.
    assert (
        store.persist(SOURCE, kind=EvidenceArtifactKind.SOURCE, media_type="text/x-python")
        == reference
    )

    path.write_bytes(b"tampered")
    with pytest.raises(RepoForgeError) as corrupt:
        store.read_range(reference.reference)
    assert corrupt.value.code is ErrorCode.STATE_CORRUPT


def test_invalid_references_and_windows_are_refused(tmp_path: Path) -> None:
    store = FileEvidenceArtifactStore(tmp_path)
    reference = store.persist(SOURCE, kind=EvidenceArtifactKind.SOURCE, media_type="text/x-python")

    for bad in (
        "",
        "evidence:source:zz",
        "failure-output:" + "a" * 64,
        "evidence:nope:" + "a" * 64,
    ):
        with pytest.raises(RepoForgeError):
            store.read_range(bad)

    with pytest.raises(RepoForgeError):
        store.read_range(reference.reference, byte_offset=len(SOURCE) + 1)
    with pytest.raises(RepoForgeError):
        store.read_range(reference.reference, max_bytes=0)
    with pytest.raises(RepoForgeError) as missing:
        store.read_range("evidence:source:" + "b" * 64)
    assert missing.value.code is ErrorCode.STATE_NOT_FOUND


def test_retention_class_is_carried_on_the_reference(tmp_path: Path) -> None:
    store = FileEvidenceArtifactStore(tmp_path)

    reference = store.persist(
        SOURCE,
        kind=EvidenceArtifactKind.CONFLICT_BODY,
        media_type="text/plain",
        retention_class=EvidenceRetentionClass.SESSION,
    )

    assert reference.retention_class is EvidenceRetentionClass.SESSION
    assert reference.reference.startswith("evidence:conflict_body:")
