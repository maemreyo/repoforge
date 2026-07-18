"""Read-only GitHub capability-probe boundary (#211).

Observes real API/permission behavior to answer per-capability availability questions
(issue/sub-issue/dependency/project read and write) without ever performing a mutating call.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from ..config import GitHubTicketGraphConfig
from ..domain.github_capability_probe import GitHubCapabilityReport


class GitHubCapabilityProbe(Protocol):
    def probe(
        self,
        cwd: Path,
        source: GitHubTicketGraphConfig | None,
    ) -> GitHubCapabilityReport: ...
