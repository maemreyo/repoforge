"""Private JSON ledger for bounded external mutation reservations."""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

from ...domain.errors import ConfigError
from ...ports.locking import LockManager

_SCHEMA_VERSION = 1
_REPO_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_MAX_RECORDS = 10_000


class JsonExternalMutationLedger:
    def __init__(self, state_root: Path, locks: LockManager) -> None:
        self.root = state_root / "external-mutation-ledger"
        self.locks = locks

    @staticmethod
    def _validate_repo_id(repo_id: str) -> str:
        if not isinstance(repo_id, str) or _REPO_ID.fullmatch(repo_id) is None:
            raise ConfigError("external mutation ledger repo_id is invalid")
        return repo_id

    @staticmethod
    def _validate_marker(marker: str) -> str:
        if (
            not isinstance(marker, str)
            or not 1 <= len(marker) <= 200
            or any(ord(character) < 32 for character in marker)
        ):
            raise ConfigError("external mutation ledger marker is invalid")
        return marker

    def _identity(self, repo_id: str) -> str:
        return hashlib.sha256(repo_id.encode("utf-8")).hexdigest()

    def _path(self, repo_id: str) -> Path:
        return self.root / f"{self._identity(repo_id)}.json"

    def _read(self, repo_id: str) -> list[dict[str, Any]]:
        path = self._path(repo_id)
        if not path.is_file():
            return []
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigError("external mutation ledger is unreadable or corrupt") from exc
        if (
            not isinstance(payload, dict)
            or payload.get("schema_version") != _SCHEMA_VERSION
            or payload.get("repo_id") != repo_id
            or not isinstance(payload.get("entries"), list)
        ):
            raise ConfigError("external mutation ledger schema is invalid")
        entries: list[dict[str, Any]] = []
        for raw in payload["entries"]:
            if not isinstance(raw, dict):
                raise ConfigError("external mutation ledger entry is invalid")
            marker = raw.get("marker")
            count = raw.get("count")
            reserved_at_epoch = raw.get("reserved_at_epoch")
            if (
                not isinstance(marker, str)
                or not isinstance(count, int)
                or isinstance(count, bool)
                or count <= 0
                or not isinstance(reserved_at_epoch, (int, float))
                or isinstance(reserved_at_epoch, bool)
            ):
                raise ConfigError("external mutation ledger entry is invalid")
            entries.append(
                {
                    "marker": self._validate_marker(marker),
                    "count": count,
                    "reserved_at_epoch": float(reserved_at_epoch),
                }
            )
        if len(entries) > _MAX_RECORDS:
            raise ConfigError("external mutation ledger exceeds its record limit")
        return entries

    def _write(self, repo_id: str, entries: list[dict[str, Any]]) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self._path(repo_id)
        temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
        encoded = (
            json.dumps(
                {
                    "schema_version": _SCHEMA_VERSION,
                    "repo_id": repo_id,
                    "entries": entries,
                },
                sort_keys=True,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
        with temporary.open("wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
        descriptor = os.open(self.root, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def reserve(
        self,
        repo_id: str,
        marker: str,
        *,
        count: int,
        now_epoch: float,
        max_in_window: int,
        window_seconds: int,
    ) -> int:
        repo_id = self._validate_repo_id(repo_id)
        marker = self._validate_marker(marker)
        if (
            not isinstance(count, int)
            or isinstance(count, bool)
            or not 1 <= count <= 20
            or not isinstance(max_in_window, int)
            or isinstance(max_in_window, bool)
            or not 1 <= max_in_window <= 10_000
            or not isinstance(window_seconds, int)
            or isinstance(window_seconds, bool)
            or not 60 <= window_seconds <= 604_800
            or not isinstance(now_epoch, (int, float))
            or isinstance(now_epoch, bool)
        ):
            raise ConfigError("external mutation ledger reservation bounds are invalid")
        lock_name = f"external-mutation-{self._identity(repo_id)[:24]}"
        with self.locks.lock(lock_name, metadata={"repo_id": repo_id}):
            cutoff = float(now_epoch) - window_seconds
            original_entries = self._read(repo_id)
            entries = [item for item in original_entries if item["reserved_at_epoch"] >= cutoff]
            current = sum(int(item["count"]) for item in entries)
            if any(item["marker"] == marker for item in entries):
                if len(entries) != len(original_entries):
                    self._write(repo_id, entries)
                return current
            if current + count > max_in_window:
                raise ConfigError(
                    "EXTERNAL_MUTATION_RATE_LIMIT: external mutation window limit would be exceeded"
                )
            entries.append(
                {
                    "marker": marker,
                    "count": count,
                    "reserved_at_epoch": float(now_epoch),
                }
            )
            if len(entries) > _MAX_RECORDS:
                raise ConfigError("external mutation ledger exceeds its record limit")
            self._write(repo_id, entries)
            return current + count
