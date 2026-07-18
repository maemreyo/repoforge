"""Persistence boundary for read-only iteration-stage cache entries."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from ..domain.verification_dag import CacheLookup, IterationCacheEntry, IterationCacheKey


class IterationCache(Protocol):
    def lookup(self, key: IterationCacheKey, *, workspace_root: Path) -> CacheLookup: ...

    def put(
        self,
        entry: IterationCacheEntry,
        *,
        protected_entry_ids: set[str] | None = None,
    ) -> IterationCacheEntry: ...

    def read(self, entry_id: str) -> IterationCacheEntry | None: ...
