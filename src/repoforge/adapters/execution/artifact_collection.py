"""Shared host-filesystem artifact collection.

Any execution backend that leaves its artifacts readable directly on the host filesystem --
native execution, or a container backend that bind-mounts the workspace read/write -- can
collect them the same way: resolve each declared relative path against the workspace root,
reject anything that is or resolves through a symlink escaping that root, and bound the size
read into memory. A container backend that instead isolated its filesystem entirely (no host
bind mount) would need a different strategy (e.g. `docker cp`); this one assumes the artifact
is already visible on the host, which holds for every backend this module currently serves.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from pathlib import Path

from ...domain.errors import SecurityError
from ...ports.execution_environment import ArtifactResult


def collect_workspace_artifacts(
    artifact_paths: Sequence[str],
    *,
    workspace_root: Path,
    max_artifact_bytes: int,
) -> tuple[ArtifactResult, ...]:
    root = workspace_root.resolve(strict=True)
    artifacts: list[ArtifactResult] = []
    for relative_path in artifact_paths:
        unresolved = workspace_root / relative_path
        if unresolved.is_symlink():
            raise SecurityError(f"Artifact path cannot be a symlink: {relative_path}")
        resolved = unresolved.resolve(strict=False)
        try:
            _ = resolved.relative_to(root)
        except ValueError as exc:
            raise SecurityError(f"Artifact path escapes workspace: {relative_path}") from exc
        if not resolved.is_file():
            continue
        size = resolved.stat().st_size
        if size > max_artifact_bytes:
            raise SecurityError(
                f"Artifact exceeds {max_artifact_bytes} byte limit: {relative_path}"
            )
        payload = resolved.read_bytes()
        artifacts.append(
            ArtifactResult(
                path=relative_path,
                size_bytes=size,
                digest=hashlib.sha256(payload).hexdigest(),
            )
        )
    return tuple(artifacts)
