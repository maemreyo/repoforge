"""Internal protocols and aggregate budgets for managed CodeGraph analysis."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ...domain.code_intelligence import CodeIntelligenceRequest
from ...domain.codegraph_config import CodeGraphOptions
from .command import CodeGraphCommandOutput
from .manifest import ProjectionResult

DEFAULT_MAX_SEED_SYMBOLS = 16
DEFAULT_MAX_TOTAL_OUTPUT_BYTES = 1_000_000
DEFAULT_MAX_WALL_SECONDS = 60.0
QUERY_RESULT_LIMIT = 10
RELATIONSHIP_COMMAND_LIMIT = 50


class ProjectionBoundary(Protocol):
    def workspace_root(self, workspace_id: str) -> Path: ...

    def prepare(
        self,
        request: CodeIntelligenceRequest,
        options: CodeGraphOptions,
    ) -> ProjectionResult: ...

    def mark_complete(self, workspace_id: str, manifest_digest: str) -> None: ...

    def invalidate(self, workspace_id: str) -> None: ...


class RunnerBoundary(Protocol):
    def init(self, projection_root: Path, home_root: Path) -> CodeGraphCommandOutput: ...

    def sync(self, projection_root: Path, home_root: Path) -> CodeGraphCommandOutput: ...

    def status(self, projection_root: Path, home_root: Path) -> CodeGraphCommandOutput: ...

    def affected(
        self,
        projection_root: Path,
        home_root: Path,
        paths: tuple[str, ...],
        *,
        depth: int | None = None,
    ) -> CodeGraphCommandOutput: ...

    def query(
        self,
        projection_root: Path,
        home_root: Path,
        search: str,
        *,
        limit: int = 20,
    ) -> CodeGraphCommandOutput: ...

    def callers(
        self,
        projection_root: Path,
        home_root: Path,
        symbol: str,
        *,
        limit: int = 50,
    ) -> CodeGraphCommandOutput: ...

    def callees(
        self,
        projection_root: Path,
        home_root: Path,
        symbol: str,
        *,
        limit: int = 50,
    ) -> CodeGraphCommandOutput: ...

    def impact(
        self,
        projection_root: Path,
        home_root: Path,
        symbol: str,
        *,
        depth: int | None = None,
    ) -> CodeGraphCommandOutput: ...


class BoundaryReached(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(slots=True)
class AnalysisBudget:
    started_at: float
    max_bytes: int
    max_seconds: float
    monotonic: Callable[[], float]
    consumed_bytes: int = 0

    def check_time(self) -> None:
        if self.monotonic() - self.started_at > self.max_seconds:
            raise BoundaryReached("CodeGraph analysis reached the reviewed wall-time bound.")

    def consume(self, output: CodeGraphCommandOutput) -> str:
        self.check_time()
        if output.truncated:
            raise BoundaryReached("CodeGraph command output reached a reviewed byte bound.")
        size = len(output.stdout.encode("utf-8"))
        if self.consumed_bytes + size > self.max_bytes:
            raise BoundaryReached("CodeGraph analysis reached the aggregate output-byte bound.")
        self.consumed_bytes += size
        return output.stdout


__all__ = [
    "DEFAULT_MAX_SEED_SYMBOLS",
    "DEFAULT_MAX_TOTAL_OUTPUT_BYTES",
    "DEFAULT_MAX_WALL_SECONDS",
    "QUERY_RESULT_LIMIT",
    "RELATIONSHIP_COMMAND_LIMIT",
    "AnalysisBudget",
    "BoundaryReached",
    "ProjectionBoundary",
    "RunnerBoundary",
]
