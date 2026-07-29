"""Operation-scoped GitHub capability preflight boundary."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from ..domain.github_capability_preflight import (
    GitHubCapabilityPreflightReport,
    GitHubCapabilityPreflightRequest,
)
from ..domain.repository_auth_broker import ProcessAuthContext


class GitHubCapabilityPreflightGateway(Protocol):
    def preflight(
        self,
        cwd: Path,
        request: GitHubCapabilityPreflightRequest,
        auth_context: ProcessAuthContext,
    ) -> GitHubCapabilityPreflightReport: ...


__all__ = ["GitHubCapabilityPreflightGateway"]
