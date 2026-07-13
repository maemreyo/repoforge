"""Backward-compatible records plus a legacy combined store/lock facade."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .bootstrap import FcntlLockManager, JsonWorkspaceStore, SystemClock
from .domain.workspace import VerificationReceipt, WorkspaceRecord


class StateStore(JsonWorkspaceStore):
    """Deprecated compatibility facade; production application injects separate ports."""

    def __init__(self, state_root: Path):
        super().__init__(state_root)
        self._legacy_locks = FcntlLockManager(state_root / "locks")

    @contextmanager
    def lock(self, workspace_id: str) -> Iterator[None]:
        with self._legacy_locks.lock(workspace_id):
            yield


def utc_now() -> str:
    return SystemClock().now_iso()


__all__ = [
    "JsonWorkspaceStore",
    "StateStore",
    "VerificationReceipt",
    "WorkspaceRecord",
    "utc_now",
]
