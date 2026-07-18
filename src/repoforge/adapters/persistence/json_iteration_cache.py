"""Private, quota-bound persistence for iteration-stage cache entries."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from ...domain.durable_state import SchemaVersion, StateCodec
from ...domain.errors import ErrorCode, RepoForgeError
from ...domain.verification_dag import (
    ITERATION_CACHE_SCHEMA_VERSION,
    CacheLookup,
    CacheMissReason,
    IterationCacheEntry,
    IterationCacheKey,
    cache_entry_payload,
    iteration_cache_entry_from_payload,
)
from ...ports.iteration_cache import IterationCache
from ...ports.locking import LockManager
from .json_state_repository import JsonStateRepository

_CACHE_ID = re.compile(r"^cache-[0-9a-f]{24}$")


def _cache_id(value: str) -> str:
    if _CACHE_ID.fullmatch(value) is None:
        raise ValueError("invalid iteration cache entry id")
    return value


class _CacheCodec(StateCodec[IterationCacheEntry]):
    schema_version = SchemaVersion(ITERATION_CACHE_SCHEMA_VERSION)

    def encode(self, value: IterationCacheEntry) -> dict[str, object]:
        return cache_entry_payload(value)

    def decode(self, payload: dict[str, object]) -> IterationCacheEntry:
        return iteration_cache_entry_from_payload(dict(payload))


class JsonIterationCache(IterationCache):
    def __init__(self, state_root: Path, locks: LockManager, *, max_entries: int = 500) -> None:
        if (
            not isinstance(max_entries, int)
            or isinstance(max_entries, bool)
            or not 1 <= max_entries <= 2_000
        ):
            raise RepoForgeError(
                "Iteration cache max_entries must be between 1 and 2000",
                code=ErrorCode.STATE_INVALID,
            )
        self._records = JsonStateRepository(
            state_root,
            collection="iteration-cache",
            locks=locks,
            codec=_CacheCodec(),
            id_validator=_cache_id,
            max_record_bytes=512_000,
        )
        self.root = self._records.root
        self.max_entries = max_entries

    def _scan(self) -> tuple[list[IterationCacheEntry], bool]:
        entries: list[IterationCacheEntry] = []
        corrupt = False
        for path in sorted(self.root.glob("cache-*.json"))[:2_000]:
            try:
                envelope = self._records.read(path.stem)
            except RepoForgeError as exc:
                if exc.code in {
                    ErrorCode.STATE_CORRUPT,
                    ErrorCode.STATE_SCHEMA_UNSUPPORTED,
                    ErrorCode.STATE_TOO_LARGE,
                }:
                    corrupt = True
                    continue
                raise
            if envelope is not None:
                entries.append(envelope.value)
        entries.sort(key=lambda item: (item.created_at, item.entry_id), reverse=True)
        return entries, corrupt

    @staticmethod
    def _artifact_status(
        entry: IterationCacheEntry, workspace_root: Path
    ) -> CacheMissReason | None:
        for artifact in entry.artifact_digests:
            candidate = workspace_root / artifact.path
            if not candidate.is_file() or candidate.is_symlink():
                return CacheMissReason.ARTIFACT_MISSING
            digest = hashlib.sha256()
            with candidate.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            if digest.hexdigest() != artifact.sha256:
                return CacheMissReason.ARTIFACT_MISMATCH
        return None

    def lookup(self, key: IterationCacheKey, *, workspace_root: Path) -> CacheLookup:
        entries, corrupt = self._scan()
        for entry in entries:
            if entry.key.cache_key != key.cache_key:
                continue
            artifact_reason = self._artifact_status(entry, workspace_root)
            if artifact_reason is not None:
                return CacheLookup(False, artifact_reason, None)
            return CacheLookup(True, None, entry)
        if corrupt:
            return CacheLookup(False, CacheMissReason.CORRUPT, None)
        return CacheLookup(False, CacheMissReason.NOT_FOUND, None)

    def _remove_corrupt(self) -> None:
        for path in sorted(self.root.glob("cache-*.json"))[:2_000]:
            try:
                self._records.read(path.stem)
            except RepoForgeError as exc:
                if exc.code in {
                    ErrorCode.STATE_CORRUPT,
                    ErrorCode.STATE_SCHEMA_UNSUPPORTED,
                    ErrorCode.STATE_TOO_LARGE,
                }:
                    self._records.delete(path.stem)
                    continue
                raise

    def put(
        self,
        entry: IterationCacheEntry,
        *,
        protected_entry_ids: set[str] | None = None,
    ) -> IterationCacheEntry:
        protected = set(protected_entry_ids or ())
        self._remove_corrupt()
        existing = self._records.read(entry.entry_id)
        if existing is None:
            try:
                self._records.create(entry.entry_id, entry)
            except RepoForgeError as exc:
                if exc.code is not ErrorCode.ALREADY_EXISTS:
                    raise
                existing = self._records.read(entry.entry_id)
                if existing is None:
                    raise RepoForgeError(
                        "Iteration cache entry disappeared after a concurrent create",
                        code=ErrorCode.STATE_STALE,
                        retryable=True,
                    ) from exc
        if existing is not None and existing.value != entry:
            raise RepoForgeError(
                "Iteration cache entry id is already bound to different content",
                code=ErrorCode.ALREADY_EXISTS,
            )
        entries, _ = self._scan()
        stale_same_key = [
            item
            for item in entries
            if item.key.cache_key == entry.key.cache_key and item.entry_id != entry.entry_id
        ]
        for item in stale_same_key:
            if item.entry_id not in protected:
                self._records.delete(item.entry_id)
        entries, _ = self._scan()
        oldest_first = sorted(entries, key=lambda item: (item.created_at, item.entry_id))
        while len(oldest_first) > self.max_entries:
            victim = next(
                (candidate for candidate in oldest_first if candidate.entry_id not in protected),
                None,
            )
            if victim is None:
                break
            self._records.delete(victim.entry_id)
            oldest_first.remove(victim)
        return entry

    def read(self, entry_id: str) -> IterationCacheEntry | None:
        try:
            envelope = self._records.read(entry_id)
        except RepoForgeError as exc:
            if exc.code in {
                ErrorCode.STATE_CORRUPT,
                ErrorCode.STATE_SCHEMA_UNSUPPORTED,
                ErrorCode.STATE_TOO_LARGE,
            }:
                return None
            raise
        return envelope.value if envelope is not None else None
