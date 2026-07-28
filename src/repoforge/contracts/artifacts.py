"""Bounded direct-to-file reporting for reviewed generated contracts."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Mapping
from pathlib import Path
from typing import TypedDict


class GeneratedArtifactRecord(TypedDict):
    path: str
    sha256: str
    changed: bool


class GeneratedArtifactReport(TypedDict):
    changed_paths: list[str]
    artifacts: list[GeneratedArtifactRecord]


def _safe_target(root: Path, relative_path: str) -> Path:
    candidate = Path(relative_path)
    if candidate.is_absolute() or not candidate.parts or ".." in candidate.parts:
        raise ValueError(f"Generated artifact path is unsafe: {relative_path!r}")
    resolved_root = root.resolve()
    resolved = (resolved_root / candidate).resolve()
    if resolved != resolved_root and resolved_root not in resolved.parents:
        raise ValueError(f"Generated artifact escapes root: {relative_path!r}")
    return resolved


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        with temporary.open("w", encoding="utf-8", newline="") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def write_generated_artifacts(
    root: Path,
    artifacts: Mapping[str, str],
) -> GeneratedArtifactReport:
    """Write reviewed artifacts atomically and return only paths, digests and change state."""

    records: list[GeneratedArtifactRecord] = []
    changed_paths: list[str] = []
    for relative_path in sorted(artifacts):
        content = artifacts[relative_path]
        target = _safe_target(root, relative_path)
        current = target.read_text(encoding="utf-8") if target.is_file() else None
        changed = current != content
        if changed:
            _atomic_write(target, content)
            changed_paths.append(relative_path)
        records.append(
            {
                "path": relative_path,
                "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                "changed": changed,
            }
        )
    return {"changed_paths": changed_paths, "artifacts": records}


__all__ = [
    "GeneratedArtifactRecord",
    "GeneratedArtifactReport",
    "write_generated_artifacts",
]
