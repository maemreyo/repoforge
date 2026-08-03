#!/usr/bin/env python3
"""Bounded, deterministic evidence emitted by RepoForge verification runners."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

_MAX_REASON_CHARS = 2_000
_MAX_LANES = 32
_MAX_ARTIFACT_BYTES = 64_000
_VALID_INTENTS = frozenset({"affected", "full", "coverage", "compatibility", "gate"})


@dataclass(frozen=True, slots=True)
class LaneTiming:
    name: str
    file_count: int
    duration_ms: float
    returncode: int

    def public(self) -> dict[str, object]:
        return {
            "name": self.name[:80],
            "file_count": max(0, self.file_count),
            "duration_ms": round(max(0.0, self.duration_ms), 3),
            "returncode": self.returncode,
        }


@dataclass(frozen=True, slots=True)
class VerificationArtifact:
    schema_version: int
    intent: str
    head_sha: str
    selected_count: int
    escalated: bool
    escalation_reason: str | None
    lanes: tuple[LaneTiming, ...]

    def public(self) -> dict[str, object]:
        if self.schema_version != 1:
            raise ValueError("verification artifact schema_version must be 1")
        if self.intent not in _VALID_INTENTS:
            raise ValueError(f"unsupported verification intent: {self.intent}")
        if len(self.head_sha) not in {40, 64} or any(
            character not in "0123456789abcdef" for character in self.head_sha.lower()
        ):
            raise ValueError("verification artifact head_sha must be a hexadecimal commit id")
        if len(self.lanes) > _MAX_LANES:
            raise ValueError(f"verification artifact supports at most {_MAX_LANES} lanes")
        reason = self.escalation_reason
        return {
            "schema_version": self.schema_version,
            "intent": self.intent,
            "head_sha": self.head_sha.lower(),
            "selected_count": max(0, self.selected_count),
            "escalated": self.escalated,
            "escalation_reason": reason[:_MAX_REASON_CHARS] if reason is not None else None,
            "lanes": [lane.public() for lane in self.lanes],
        }


def write_artifact(path: Path, artifact: VerificationArtifact) -> None:
    """Atomically write canonical JSON while enforcing a hard output budget."""

    encoded = (json.dumps(artifact.public(), sort_keys=True, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )
    if len(encoded) > _MAX_ARTIFACT_BYTES:
        raise ValueError("verification artifact exceeds 64 KB")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        temporary_path.replace(path)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise
