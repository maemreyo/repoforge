"""Bounded local read-through cache boundary for already-sanitized GitHub reads.

Implementations must never raise: a stale (TTL-expired) or corrupt entry is
equivalent to a cache miss, and a failed write is a silent no-op. The cache
is evidence only. It never grants authorization, and repository/path/branch
policy is enforced identically for a cached or a freshly read payload.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol


class GitHubReadCache(Protocol):
    def get(
        self,
        repo_id: str,
        repo_path: Path,
        kind: str,
        number: int,
        *,
        ttl_seconds: int,
        now_epoch: float,
    ) -> dict[str, Any] | None: ...

    def put(
        self,
        repo_id: str,
        repo_path: Path,
        kind: str,
        number: int,
        payload: dict[str, Any],
        *,
        now_epoch: float,
    ) -> None: ...

    def invalidate(
        self,
        repo_id: str,
        repo_path: Path,
        *,
        kind: str | None = None,
    ) -> int: ...
