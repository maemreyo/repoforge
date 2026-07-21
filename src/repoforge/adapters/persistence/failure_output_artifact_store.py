"""Content-addressed persistence for complete, secret-safe failure output."""

from __future__ import annotations

import hashlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from ...domain.redaction import redact_text

_MAX_FAILURE_OUTPUT_ARTIFACT_BYTES = 10 * 1024 * 1024
_MAX_SECRET_SAFE_ARTIFACT_CHARS = 1_000_000
_MAX_SECRET_SAFE_ARTIFACT_LINES = 20_000


@dataclass(frozen=True, slots=True)
class FailureOutputArtifact:
    reference: str | None
    status: str


def persist_failure_output(state_root: Path, content: str) -> FailureOutputArtifact:
    """Persist one bounded redacted artifact without leaking raw failure output."""

    raw_bytes = content.encode("utf-8", errors="replace")
    if (
        len(raw_bytes) > _MAX_FAILURE_OUTPUT_ARTIFACT_BYTES
        or len(content) > _MAX_SECRET_SAFE_ARTIFACT_CHARS
        or content.count("\n") + 1 > _MAX_SECRET_SAFE_ARTIFACT_LINES
    ):
        return FailureOutputArtifact(None, "oversized")
    safe = redact_text(content, limit=2_000_000)
    if (
        len(safe) > _MAX_SECRET_SAFE_ARTIFACT_CHARS
        or safe.count("\n") + 1 > _MAX_SECRET_SAFE_ARTIFACT_LINES
    ):
        return FailureOutputArtifact(None, "oversized")
    payload = safe.encode("utf-8", errors="replace")
    digest = hashlib.sha256(payload).hexdigest()
    root = state_root / "failure-output-artifacts"
    try:
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(root, 0o700)
        target = root / f"{digest}.blob"
        if target.is_symlink():
            return FailureOutputArtifact(None, "persistence_failed")
        if target.exists():
            existing = target.read_bytes()
            if existing != payload or hashlib.sha256(existing).hexdigest() != digest:
                return FailureOutputArtifact(None, "persistence_failed")
        else:
            descriptor, temporary_name = tempfile.mkstemp(prefix=f".{digest}.tmp-", dir=root)
            temporary = Path(temporary_name)
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    os.fchmod(handle.fileno(), 0o600)
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, target)
                os.chmod(target, 0o600)
            finally:
                temporary.unlink(missing_ok=True)
    except OSError:
        return FailureOutputArtifact(None, "persistence_failed")
    return FailureOutputArtifact(f"failure-output:{digest}", "available")
