"""Crash-safe, revision-aware JSON workspace registry."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path

from ...domain.errors import WorkspaceError
from ...domain.workspace import VerificationReceipt, WorkspaceRecord
from ...ports.locking import LockManager
from ..filesystem.atomic import atomic_write_text, fsync_dir
from ..locking.fcntl import FcntlLockManager

_REVISION_ATTRIBUTE = "_registry_revision"


class JsonWorkspaceStore:
    def __init__(self, state_root: Path, locks: LockManager | None = None):
        self.root = state_root
        self.registry_dir = self.root / "workspaces"
        self.registry_dir.mkdir(parents=True, exist_ok=True)
        self._locks = locks or FcntlLockManager(self.root / "locks")

    def _record_path(self, workspace_id: str) -> Path:
        if not workspace_id or any(
            c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_:"
            for c in workspace_id
        ):
            raise WorkspaceError(f"Invalid workspace id: {workspace_id!r}")
        return self.registry_dir / f"{workspace_id}.json"

    def _lock_name(self, workspace_id: str) -> str:
        self._record_path(workspace_id)
        return f"workspace-registry-{workspace_id}"

    @staticmethod
    def _set_revision(record: WorkspaceRecord, revision: int) -> None:
        setattr(record, _REVISION_ATTRIBUTE, revision)

    @staticmethod
    def _expected_revision(record: WorkspaceRecord) -> int | None:
        value = getattr(record, _REVISION_ATTRIBUTE, None)
        return (
            value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None
        )

    def _read_unlocked(self, workspace_id: str) -> tuple[WorkspaceRecord, int]:
        path = self._record_path(workspace_id)
        if not path.is_file():
            raise WorkspaceError(f"Unknown workspace id: {workspace_id}")
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise TypeError("record must be a JSON object")
            revision_raw = raw.pop("revision", 1)
            if (
                not isinstance(revision_raw, int)
                or isinstance(revision_raw, bool)
                or revision_raw <= 0
            ):
                raise ValueError("revision must be a positive integer")
            receipt_raw = raw.pop("last_verification", None)
            receipt = VerificationReceipt(**receipt_raw) if receipt_raw else None
            record = WorkspaceRecord(last_verification=receipt, **raw)
            self._set_revision(record, revision_raw)
            return record, revision_raw
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise WorkspaceError(f"Invalid workspace registry record {path}: {exc}") from exc

    def _write_unlocked(self, record: WorkspaceRecord, revision: int) -> None:
        payload = asdict(record)
        payload["revision"] = revision
        atomic_write_text(
            self._record_path(record.workspace_id),
            json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        )
        self._set_revision(record, revision)

    def save(self, record: WorkspaceRecord) -> None:
        destination = self._record_path(record.workspace_id)
        with self._locks.lock(
            self._lock_name(record.workspace_id),
            timeout_seconds=5,
            metadata={"operation": "workspace_registry_save"},
        ):
            if destination.is_file():
                _current, current_revision = self._read_unlocked(record.workspace_id)
                expected_revision = self._expected_revision(record)
                if expected_revision != current_revision:
                    raise WorkspaceError(
                        "Workspace registry record changed; reload it before saving"
                    )
                next_revision = current_revision + 1
            else:
                if self._expected_revision(record) is not None:
                    raise WorkspaceError("Workspace registry record disappeared before save")
                next_revision = 1
            self._write_unlocked(record, next_revision)

    def load(self, workspace_id: str) -> WorkspaceRecord:
        record, _revision = self._read_unlocked(workspace_id)
        return record

    def update(
        self,
        workspace_id: str,
        updater: Callable[[WorkspaceRecord], None],
    ) -> WorkspaceRecord:
        with self._locks.lock(
            self._lock_name(workspace_id),
            timeout_seconds=5,
            metadata={"operation": "workspace_registry_update"},
        ):
            record, revision = self._read_unlocked(workspace_id)
            updater(record)
            self._write_unlocked(record, revision + 1)
            return record

    def delete(self, workspace_id: str) -> None:
        destination = self._record_path(workspace_id)
        with self._locks.lock(
            self._lock_name(workspace_id),
            timeout_seconds=5,
            metadata={"operation": "workspace_registry_delete"},
        ):
            destination.unlink(missing_ok=True)
            fsync_dir(destination.parent)

    def list(self) -> list[WorkspaceRecord]:
        records: list[WorkspaceRecord] = []
        for path in sorted(self.registry_dir.glob("*.json")):
            try:
                records.append(self.load(path.stem))
            except WorkspaceError:
                continue
        return records
