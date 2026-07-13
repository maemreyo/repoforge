"""GitHub issue and pull-request boundary."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

from ..config import RepositoryConfig


class PullRequestGateway(Protocol):
    def auth_status(self, cwd: Path) -> tuple[bool, str]: ...

    def issue_read(self, cwd: Path, issue_number: int) -> dict[str, Any]: ...

    def pr_read(self, cwd: Path, pr_number: int) -> dict[str, Any]: ...

    def find_pr(self, cwd: Path, branch: str) -> dict[str, Any] | None: ...

    def create_draft(
        self,
        cwd: Path,
        repo: RepositoryConfig,
        *,
        branch: str,
        base: str,
        title: str,
        body: str,
    ) -> str: ...

    def update(
        self, cwd: Path, branch: str, *, title: str | None, body: str | None
    ) -> dict[str, Any]: ...

    def status(self, cwd: Path, branch: str) -> dict[str, Any]: ...

    def checks(self, cwd: Path, branch: str, *, required_only: bool) -> list[dict[str, Any]]: ...
