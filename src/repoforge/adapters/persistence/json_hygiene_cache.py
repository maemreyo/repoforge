"""Checksum-framed private cache for exact-base hygiene findings."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from ...domain.hygiene import HygieneFinding
from ...ports.hygiene import HygieneCacheKey
from ...ports.locking import LockManager

_SCHEMA_VERSION = 1
_MAX_ENTRIES = 64
_MAX_FINDINGS = 2_000
_MAX_ENTRY_BYTES = 1_000_000


def _key_payload(key: HygieneCacheKey) -> dict[str, object]:
    return {
        "base_sha": key.base_sha,
        "config_identity": key.config_identity,
        "environment_identity": key.environment_identity,
        "formatter_contract_hash": key.formatter_contract_hash,
        "repo_id": key.repo_id,
        "ttl_seconds": key.ttl_seconds,
    }


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()


class JsonHygieneBaselineCache:
    def __init__(self, state_root: Path, locks: LockManager) -> None:
        self._path = state_root.expanduser().resolve() / "hygiene-baseline-cache.json"
        self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self._path.parent, 0o700)
        self._locks = locks

    @staticmethod
    def _empty() -> dict[str, Any]:
        return {"version": _SCHEMA_VERSION, "entries": {}}

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

    def _load(self) -> dict[str, Any]:
        if not self._path.is_file():
            return self._empty()
        try:
            raw: Any = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return self._empty()
        if (
            not isinstance(raw, dict)
            or raw.get("version") != _SCHEMA_VERSION
            or not isinstance(raw.get("entries"), dict)
        ):
            return self._empty()
        return raw

    def _write(self, document: dict[str, Any]) -> None:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{self._path.name}.tmp-",
            dir=self._path.parent,
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                os.fchmod(handle.fileno(), 0o600)
                json.dump(document, handle, sort_keys=True, ensure_ascii=False)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self._path)
            os.chmod(self._path, 0o600)
            self._fsync_dir(self._path.parent)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _cache_id(key: HygieneCacheKey) -> str:
        return _digest(_key_payload(key))

    def get(
        self,
        key: HygieneCacheKey,
        *,
        now_epoch: float,
    ) -> tuple[HygieneFinding, ...] | None:
        try:
            with self._locks.lock("hygiene-baseline-cache", timeout_seconds=2):
                document = self._load()
                entry = document["entries"].get(self._cache_id(key))
                if not isinstance(entry, dict):
                    return None
                stored_at = entry.get("stored_at")
                framed = entry.get("frame")
                checksum = entry.get("checksum")
                if (
                    not isinstance(stored_at, (int, float))
                    or isinstance(stored_at, bool)
                    or not isinstance(framed, dict)
                    or not isinstance(checksum, str)
                    or _digest(framed) != checksum
                    or framed.get("key") != _key_payload(key)
                ):
                    return None
                if float(now_epoch) - float(stored_at) > float(key.ttl_seconds):
                    return None
                raw_findings = framed.get("findings")
                if not isinstance(raw_findings, list) or len(raw_findings) > _MAX_FINDINGS:
                    return None
                findings: list[HygieneFinding] = []
                for raw in raw_findings:
                    if not isinstance(raw, dict):
                        return None
                    path = raw.get("path")
                    rule = raw.get("rule")
                    message = raw.get("message")
                    if (
                        not isinstance(path, str)
                        or not isinstance(rule, str)
                        or not isinstance(message, str)
                    ):
                        return None
                    findings.append(HygieneFinding.create(path, rule, message))
                return tuple(sorted(set(findings)))
        except Exception:
            return None

    def put(
        self,
        key: HygieneCacheKey,
        findings: tuple[HygieneFinding, ...],
        *,
        now_epoch: float,
    ) -> None:
        try:
            bounded = tuple(sorted(set(findings)))
            if len(bounded) > _MAX_FINDINGS:
                return
            frame = {
                "findings": [
                    {"message": item.message, "path": item.path, "rule": item.rule}
                    for item in bounded
                ],
                "key": _key_payload(key),
            }
            encoded = json.dumps(frame, sort_keys=True, ensure_ascii=False).encode("utf-8")
            if len(encoded) > _MAX_ENTRY_BYTES:
                return
            with self._locks.lock("hygiene-baseline-cache", timeout_seconds=2):
                document = self._load()
                entries = document["entries"]
                entries[self._cache_id(key)] = {
                    "checksum": _digest(frame),
                    "frame": frame,
                    "stored_at": float(now_epoch),
                }
                if len(entries) > _MAX_ENTRIES:
                    ordered = sorted(
                        entries.items(),
                        key=lambda item: (
                            float(item[1].get("stored_at", 0.0))
                            if isinstance(item[1], dict)
                            else 0.0
                        ),
                    )
                    for cache_id, _ in ordered[: len(entries) - _MAX_ENTRIES]:
                        entries.pop(cache_id, None)
                document["version"] = _SCHEMA_VERSION
                self._write(document)
        except Exception:
            return
