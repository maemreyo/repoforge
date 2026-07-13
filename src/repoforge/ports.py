"""Structural contracts used by application services."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any, Protocol

from .runner import CommandResult
from .state import WorkspaceRecord


class CommandExecutor(Protocol):
    """Constrained command execution used by workspace application logic."""

    def environment(self, extra: Mapping[str, str] | None = None) -> dict[str, str]: ...

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        input_text: str | None = None,
        timeout: int | None = None,
        check: bool = True,
        extra_env: Mapping[str, str] | None = None,
        output_limit: int | None = None,
    ) -> CommandResult: ...

    def run_bytes(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        timeout: int | None = None,
        max_bytes: int,
    ) -> bytes: ...


class WorkspaceStore(Protocol):
    """Persistent workspace records with mutual exclusion per workspace identifier."""

    def save(self, record: WorkspaceRecord) -> None: ...

    def load(self, workspace_id: str) -> WorkspaceRecord: ...

    def delete(self, workspace_id: str) -> None: ...

    def list(self) -> list[WorkspaceRecord]: ...

    def lock(self, workspace_id: str) -> AbstractContextManager[None]: ...


class AuditSink(Protocol):
    """Minimal audit boundary that excludes process output and file content."""

    @property
    def path(self) -> Path: ...

    def record(self, action: str, *, success: bool, details: dict[str, Any]) -> None: ...
