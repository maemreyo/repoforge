"""Crash-safe JSON workspace registry; locking is a separate port."""

from __future__ import annotations

import json
import os
from dataclasses import asdict
from pathlib import Path

from ...domain.errors import WorkspaceError
from ...domain.workspace import VerificationReceipt, WorkspaceRecord


class JsonWorkspaceStore:
    def __init__(self, state_root: Path):
        self.root = state_root
        self.registry_dir = self.root / "workspaces"
        self.registry_dir.mkdir(parents=True, exist_ok=True)

    def _record_path(self, workspace_id: str) -> Path:
        if not workspace_id or any(
            c not in "abcdefghijklmnopqrstuvwxyz0123456789-" for c in workspace_id
        ):
            raise WorkspaceError(f"Invalid workspace id: {workspace_id!r}")
        return self.registry_dir / f"{workspace_id}.json"

    @staticmethod
    def _fsync_dir(path: Path) -> None:
        try:
            fd = os.open(path, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def save(self, record: WorkspaceRecord) -> None:
        destination = self._record_path(record.workspace_id)
        temporary = destination.with_name(
            f".{destination.name}.tmp-{os.getpid()}-{os.urandom(4).hex()}"
        )
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(asdict(record), indent=2, sort_keys=True, ensure_ascii=False) + "\n"
                )
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
            self._fsync_dir(destination.parent)
        finally:
            temporary.unlink(missing_ok=True)

    def load(self, workspace_id: str) -> WorkspaceRecord:
        path = self._record_path(workspace_id)
        if not path.is_file():
            raise WorkspaceError(f"Unknown workspace id: {workspace_id}")
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            receipt_raw = raw.pop("last_verification", None)
            receipt = VerificationReceipt(**receipt_raw) if receipt_raw else None
            return WorkspaceRecord(last_verification=receipt, **raw)
        except (OSError, ValueError, TypeError) as exc:
            raise WorkspaceError(f"Invalid workspace registry record {path}: {exc}") from exc

    def delete(self, workspace_id: str) -> None:
        destination = self._record_path(workspace_id)
        destination.unlink(missing_ok=True)
        self._fsync_dir(destination.parent)

    def list(self) -> list[WorkspaceRecord]:
        records: list[WorkspaceRecord] = []
        for path in sorted(self.registry_dir.glob("*.json")):
            try:
                records.append(self.load(path.stem))
            except WorkspaceError:
                continue
        return records
