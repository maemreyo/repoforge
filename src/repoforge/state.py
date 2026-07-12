"""Workspace registry and Unix file locks."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .errors import WorkspaceError

try:
    import fcntl
except ImportError as exc:  # pragma: no cover - this project targets macOS/Linux
    raise RuntimeError("repoforge-mcp requires a Unix-like platform with fcntl") from exc


@dataclass
class VerificationReceipt:
    profile: str
    fingerprint: str
    completed_at: str
    commands: list[dict[str, Any]]


@dataclass
class WorkspaceRecord:
    workspace_id: str
    repo_id: str
    path: str
    branch: str
    base: str
    remote: str
    created_at: str
    last_verification: VerificationReceipt | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class StateStore:
    def __init__(self, state_root: Path):
        self.root = state_root
        self.registry_dir = self.root / "workspaces"
        self.lock_dir = self.root / "locks"
        self.registry_dir.mkdir(parents=True, exist_ok=True)
        self.lock_dir.mkdir(parents=True, exist_ok=True)

    def _record_path(self, workspace_id: str) -> Path:
        if not workspace_id or any(
            ch not in "abcdefghijklmnopqrstuvwxyz0123456789-" for ch in workspace_id
        ):
            raise WorkspaceError(f"Invalid workspace id: {workspace_id!r}")
        return self.registry_dir / f"{workspace_id}.json"

    def save(self, record: WorkspaceRecord) -> None:
        destination = self._record_path(record.workspace_id)
        temporary = destination.with_suffix(f".tmp-{os.getpid()}")
        temporary.write_text(
            json.dumps(asdict(record), indent=2, sort_keys=True, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, destination)

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
        path = self._record_path(workspace_id)
        if path.exists():
            path.unlink()

    def list(self) -> list[WorkspaceRecord]:
        records: list[WorkspaceRecord] = []
        for path in sorted(self.registry_dir.glob("*.json")):
            try:
                records.append(self.load(path.stem))
            except WorkspaceError:
                continue
        return records

    @contextmanager
    def lock(self, workspace_id: str) -> Iterator[None]:
        lock_path = self.lock_dir / f"{workspace_id}.lock"
        with lock_path.open("a+") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
