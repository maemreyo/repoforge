"""Workspace record persistence and locking boundary."""

from __future__ import annotations
from contextlib import AbstractContextManager
from typing import Protocol
from ..domain.workspace import WorkspaceRecord


class WorkspaceStore(Protocol):
    def save(self, record: WorkspaceRecord) -> None: ...

    def load(self, workspace_id: str) -> WorkspaceRecord: ...

    def delete(self, workspace_id: str) -> None: ...

    def list(self) -> list[WorkspaceRecord]: ...

    def lock(self, workspace_id: str) -> AbstractContextManager[None]: ...
