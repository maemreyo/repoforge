"""Bounded disposal and startup cleanup for managed CodeGraph state."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class ProjectionLifecycle(Protocol):
    def dispose_workspace(self, workspace_id: str) -> None: ...

    def cleanup_workspaces(
        self,
        active_workspace_ids: frozenset[str],
        *,
        limit: int,
    ) -> tuple[int, int, int]: ...


@dataclass(frozen=True, slots=True)
class CodeGraphCleanupResult:
    removed: int
    skipped: int
    incomplete: int

    def __post_init__(self) -> None:
        if any(
            not isinstance(value, int) or isinstance(value, bool) or value < 0
            for value in (self.removed, self.skipped, self.incomplete)
        ):
            raise ValueError("cleanup counts must be non-negative integers")


class CodeGraphLifecycle:
    def __init__(self, projection: ProjectionLifecycle) -> None:
        self._projection = projection

    def dispose_workspace(self, workspace_id: str) -> None:
        if not isinstance(workspace_id, str) or not workspace_id:
            raise ValueError("workspace_id must be non-empty text")
        self._projection.dispose_workspace(workspace_id)

    def startup_cleanup(
        self,
        active_workspace_ids: frozenset[str],
        *,
        limit: int = 100,
    ) -> CodeGraphCleanupResult:
        if not isinstance(active_workspace_ids, frozenset) or any(
            not isinstance(workspace_id, str) or not workspace_id
            for workspace_id in active_workspace_ids
        ):
            raise ValueError("active_workspace_ids must be normalized text identities")
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1 or limit > 10_000:
            raise ValueError("cleanup limit must be an integer between 1 and 10000")
        removed, skipped, incomplete = self._projection.cleanup_workspaces(
            active_workspace_ids,
            limit=limit,
        )
        return CodeGraphCleanupResult(removed, skipped, incomplete)


__all__ = ["CodeGraphCleanupResult", "CodeGraphLifecycle", "ProjectionLifecycle"]
