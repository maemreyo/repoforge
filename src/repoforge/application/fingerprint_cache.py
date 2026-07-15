"""Lock-scoped in-memory workspace fingerprint cache."""

from __future__ import annotations

import hashlib
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..ports.git import GitRepository


@dataclass(frozen=True, slots=True)
class CachedFingerprint:
    fingerprint: str
    validity_token: str


@dataclass(frozen=True, slots=True)
class FingerprintLookup:
    fingerprint: str
    source: str
    duration_ms: float


class FingerprintCache:
    """Stores workspace fingerprints guarded by the caller's workspace lock."""

    def __init__(self) -> None:
        self._data: dict[str, CachedFingerprint] = {}
        self._lock = threading.Lock()

    def get(self, workspace_id: str) -> CachedFingerprint | None:
        with self._lock:
            return self._data.get(workspace_id)

    def set(self, workspace_id: str, fingerprint: str, validity_token: str) -> None:
        with self._lock:
            self._data[workspace_id] = CachedFingerprint(fingerprint, validity_token)

    def invalidate(self, workspace_id: str) -> None:
        with self._lock:
            self._data.pop(workspace_id, None)


def _status_path_metadata(path: Path, status: bytes) -> bytes:
    digest = hashlib.sha256()
    for entry in (item for item in status.split(b"\0") if item):
        if entry.startswith((b"1 ", b"2 ", b"u ", b"? ")):
            raw_path = entry.split(b" ")[-1]
            candidate = path / os.fsdecode(raw_path)
            try:
                metadata = candidate.lstat()
            except FileNotFoundError:
                digest.update(b"\0missing\0")
            else:
                digest.update(
                    f"{metadata.st_mode}:{metadata.st_size}:{metadata.st_mtime_ns}".encode()
                )
        digest.update(b"\0")
    return digest.digest()


def compute_validity_token(git: GitRepository, path: Path) -> str:
    status = git.status_porcelain_v2(path)
    digest = hashlib.sha256()
    digest.update(git.head_sha(path).encode())
    digest.update(status)
    digest.update(_status_path_metadata(path, status))
    return digest.hexdigest()


def read_fingerprint(
    cache: FingerprintCache | None,
    workspace_id: str,
    git: GitRepository,
    path: Path,
) -> FingerprintLookup:
    started = time.monotonic()
    existing = cache.get(workspace_id) if cache is not None else None
    if existing is not None and compute_validity_token(git, path) == existing.validity_token:
        return FingerprintLookup(
            existing.fingerprint,
            "cache_hit",
            round((time.monotonic() - started) * 1000, 3),
        )
    if cache is not None:
        cache.invalidate(workspace_id)
    fingerprint = git.fingerprint(path)
    if cache is not None:
        cache.set(workspace_id, fingerprint, compute_validity_token(git, path))
    return FingerprintLookup(
        fingerprint,
        "computed",
        round((time.monotonic() - started) * 1000, 3),
    )


def prime_fingerprint(
    cache: FingerprintCache | None,
    workspace_id: str,
    git: GitRepository,
    path: Path,
) -> FingerprintLookup:
    started = time.monotonic()
    fingerprint = git.fingerprint(path)
    if cache is not None:
        cache.set(workspace_id, fingerprint, compute_validity_token(git, path))
    return FingerprintLookup(
        fingerprint,
        "computed",
        round((time.monotonic() - started) * 1000, 3),
    )
