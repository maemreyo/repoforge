"""Provider-owned workspace state lifecycle boundary."""

from __future__ import annotations

from typing import Protocol


class ProviderWorkspaceLifecycle(Protocol):
    def dispose_workspace(self, workspace_id: str) -> None: ...


__all__ = ["ProviderWorkspaceLifecycle"]
