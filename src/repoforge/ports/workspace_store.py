"""Workspace record persistence boundary.

Mutual exclusion is intentionally modeled by :mod:`repoforge.ports.locking` so storage adapters stay
portable and application tests can inject persistence failures independently from lock failures.
"""

from __future__ import annotations

from typing import Protocol

from ..domain.workspace import WorkspaceRecord


class WorkspaceStore(Protocol):
    def save(self, record: WorkspaceRecord) -> None: ...

    def load(self, workspace_id: str) -> WorkspaceRecord: ...

    def delete(self, workspace_id: str) -> None: ...

    def list(self) -> list[WorkspaceRecord]: ...
