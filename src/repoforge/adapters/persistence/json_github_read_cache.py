"""Private bounded TTL cache for already-sanitized GitHub issue/PR reads.

Stores one small, private, atomic, lock-guarded JSON file under the state
root -- the same single-file pattern used by ``operation-metrics.json`` --
rather than one file per cached resource. The cache is evidence only: it
never grants authorization, and a stale (TTL-expired), corrupt, or
oversized entry is always treated as a miss so callers fall back to a live
read without raising.

Eviction is persistent least-recently-used: every valid hit refreshes a
small ``last_accessed_at`` field under the cache lock, while TTL freshness
continues to be measured from ``stored_at`` so repeated reads never extend
the lifetime of stale GitHub evidence.

The cache key binds a repository-path fingerprint alongside ``repo_id``: a
config's ``repo_id`` label is only a name, and if it is ever repointed at a
different local checkout (a config edit, reload, or repository migration)
while an old entry is still within its TTL, the label alone would let a
stale cross-repository payload be served under the new repository's
issue/PR number. Hashing the resolved repository path into the key makes
that repointing a guaranteed cache miss instead.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from ...ports.locking import LockManager

_SCHEMA_VERSION = 1
DEFAULT_MAX_ENTRIES = 128
DEFAULT_MAX_ENTRY_BYTES = 500_000
_SAFE_KEY_PART = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


class JsonGitHubReadCache:
    def __init__(
        self,
        state_root: Path,
        locks: LockManager,
        *,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        max_entry_bytes: int = DEFAULT_MAX_ENTRY_BYTES,
    ) -> None:
        self._path = state_root.expanduser().resolve() / "github-read-cache.json"
        self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self._path.parent, 0o700)
        self._locks = locks
        self._max_entries = max(1, int(max_entries))
        self._max_entry_bytes = max(1, int(max_entry_bytes))

    @staticmethod
    def _fsync_dir(path: Path) -> None:
        try:
            descriptor = os.open(path, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(descriptor)
        except OSError:
            pass
        finally:
            os.close(descriptor)

    @staticmethod
    def _key(repo_id: str, repo_path: Path, kind: str, number: int) -> str | None:
        if (
            not isinstance(repo_id, str)
            or not isinstance(kind, str)
            or not _SAFE_KEY_PART.fullmatch(repo_id)
            or not _SAFE_KEY_PART.fullmatch(kind)
            or not isinstance(number, int)
            or isinstance(number, bool)
            or number <= 0
        ):
            return None
        try:
            resolved_path = str(Path(repo_path).resolve())
        except OSError:
            return None
        fingerprint = hashlib.sha256(resolved_path.encode("utf-8")).hexdigest()[:16]
        return f"{repo_id}:{fingerprint}:{kind}:{number}"

    @staticmethod
    def _empty() -> dict[str, Any]:
        return {"version": _SCHEMA_VERSION, "entries": {}}

    @staticmethod
    def _entry_recency(item: tuple[str, Any]) -> float:
        raw = item[1]
        if not isinstance(raw, dict):
            return 0.0
        value = raw.get("last_accessed_at", raw.get("stored_at", 0.0))
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return 0.0
        return float(value)

    def _load(self) -> dict[str, Any]:
        if not self._path.is_file():
            return self._empty()
        try:
            raw: Any = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return self._empty()
        if not isinstance(raw, dict) or not isinstance(raw.get("entries"), dict):
            return self._empty()
        return raw

    def _write(self, payload: dict[str, Any]) -> None:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{self._path.name}.tmp-",
            dir=self._path.parent,
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                os.fchmod(handle.fileno(), 0o600)
                json.dump(payload, handle, sort_keys=True, ensure_ascii=False)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self._path)
            os.chmod(self._path, 0o600)
            self._fsync_dir(self._path.parent)
        finally:
            temporary.unlink(missing_ok=True)

    def get(
        self,
        repo_id: str,
        repo_path: Path,
        kind: str,
        number: int,
        *,
        ttl_seconds: int,
        now_epoch: float,
    ) -> dict[str, Any] | None:
        try:
            key = self._key(repo_id, repo_path, kind, number)
            if key is None:
                return None
            with self._locks.lock("github-read-cache", timeout_seconds=2):
                document = self._load()
                entry = document["entries"].get(key)
                if not isinstance(entry, dict):
                    return None
                stored_at = entry.get("stored_at")
                payload = entry.get("payload")
                if (
                    not isinstance(stored_at, (int, float))
                    or isinstance(stored_at, bool)
                    or not isinstance(payload, dict)
                ):
                    return None
                age = float(now_epoch) - float(stored_at)
                if age > float(ttl_seconds):
                    return None
                entry["last_accessed_at"] = float(now_epoch)
                document["version"] = _SCHEMA_VERSION
                self._write(document)
                return dict(payload)
        except Exception:
            # A corrupt or otherwise unreadable cache entry is exactly a cache
            # miss: the caller always falls back to a live read, never an error.
            return None

    def put(
        self,
        repo_id: str,
        repo_path: Path,
        kind: str,
        number: int,
        payload: dict[str, Any],
        *,
        now_epoch: float,
    ) -> None:
        try:
            key = self._key(repo_id, repo_path, kind, number)
            if key is None or not isinstance(payload, dict):
                return
            try:
                encoded = json.dumps(payload, ensure_ascii=False)
            except (TypeError, ValueError):
                return
            if len(encoded.encode("utf-8")) > self._max_entry_bytes:
                return
            with self._locks.lock("github-read-cache", timeout_seconds=2):
                document = self._load()
                entries = document["entries"]
                entries[key] = {
                    "payload": payload,
                    "stored_at": float(now_epoch),
                    "last_accessed_at": float(now_epoch),
                }
                if len(entries) > self._max_entries:
                    ordered = sorted(entries.items(), key=self._entry_recency)
                    overflow = len(entries) - self._max_entries
                    for stale_key, _ in ordered[:overflow]:
                        entries.pop(stale_key, None)
                document["version"] = _SCHEMA_VERSION
                self._write(document)
        except Exception:
            # Caching is best-effort evidence only; a persistence failure must
            # never break the live read that produced this payload.
            return

    def invalidate(
        self,
        repo_id: str,
        repo_path: Path,
        *,
        kind: str | None = None,
    ) -> int:
        """Remove repository-scoped evidence; failures are a safe no-op."""
        try:
            if not isinstance(repo_id, str) or not _SAFE_KEY_PART.fullmatch(repo_id):
                return 0
            if kind is not None and (
                not isinstance(kind, str) or not _SAFE_KEY_PART.fullmatch(kind)
            ):
                return 0
            resolved_path = str(Path(repo_path).resolve())
            fingerprint = hashlib.sha256(resolved_path.encode("utf-8")).hexdigest()[:16]
            prefix = f"{repo_id}:{fingerprint}:"
            kind_prefix = f"{prefix}{kind}:" if kind is not None else prefix
            with self._locks.lock("github-read-cache", timeout_seconds=2):
                document = self._load()
                entries = document["entries"]
                matches = [key for key in entries if key.startswith(kind_prefix)]
                for key in matches:
                    entries.pop(key, None)
                if matches:
                    document["version"] = _SCHEMA_VERSION
                    self._write(document)
                return len(matches)
        except Exception:
            return 0
