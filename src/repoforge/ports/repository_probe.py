"""Read-only local repository inspection boundary."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from ..domain.repository_detection import RepositoryFacts


class RepositoryProbe(Protocol):
    def inspect(self, path: Path, *, repo_id: str | None = None) -> RepositoryFacts: ...
