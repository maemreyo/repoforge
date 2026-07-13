"""Crash-safe JSON workspace registry with per-workspace Unix locks."""

from __future__ import annotations
import json
import os
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from collections.abc import Iterator
from ...domain.errors import WorkspaceError
from ...domain.workspace import VerificationReceipt, WorkspaceRecord

try:
    import fcntl
except ImportError as exc:
    raise RuntimeError(
        "repoforge-mcp requires a Unix-like platform with fcntl"
    ) from exc


class JsonWorkspaceStore:
    def __init__(self, state_root: Path):
        self.root = state_root
        self.registry_dir = self.root / "workspaces"
        self.lock_dir = self.root / "locks"
        self.registry_dir.mkdir(parents=True, exist_ok=True)
        self.lock_dir.mkdir(parents=True, exist_ok=True)

    def _record_path(self, workspace_id: str) -> Path:
        if not workspace_id or any(
            (c not in "abcdefghijklmnopqrstuvwxyz0123456789-" for c in workspace_id)
        ):
            raise WorkspaceError(f"Invalid workspace id: {workspace_id!r}")
        return self.registry_dir / f"{workspace_id}.json"

    def save(self, record: WorkspaceRecord) -> None:
        destination = self._record_path(record.workspace_id)
        temporary = destination.with_name(f".{destination.name}.tmp-{os.getpid()}")
        try:
            with temporary.open("w", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        asdict(record), indent=2, sort_keys=True, ensure_ascii=False
                    )
                    + "\n"
                )
                handle.flush()
                os.fsync(handle.fileno())
            temporary.chmod(384)
            os.replace(temporary, destination)
            try:
                fd = os.open(destination.parent, os.O_RDONLY)
                os.fsync(fd)
                os.close(fd)
            except OSError:
                pass
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
            raise WorkspaceError(
                f"Invalid workspace registry record {path}: {exc}"
            ) from exc

    def delete(self, workspace_id: str) -> None:
        self._record_path(workspace_id).unlink(missing_ok=True)

    def list(self) -> list[WorkspaceRecord]:
        records = []
        for path in sorted(self.registry_dir.glob("*.json")):
            try:
                records.append(self.load(path.stem))
            except WorkspaceError:
                continue
        return records

    @contextmanager
    def lock(self, workspace_id: str) -> Iterator[None]:
        path = self.lock_dir / f"{workspace_id}.lock"
        with path.open("a+") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


StateStore = JsonWorkspaceStore
