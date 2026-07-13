"""Backward-compatible workspace record and store imports."""

from .adapters.persistence import JsonWorkspaceStore, StateStore
from .adapters.system import SystemClock
from .domain.workspace import VerificationReceipt, WorkspaceRecord


def utc_now() -> str:
    return SystemClock().now_iso()


__all__ = [
    "JsonWorkspaceStore",
    "StateStore",
    "VerificationReceipt",
    "WorkspaceRecord",
    "utc_now",
]
