"""Read-only boundary for one GitHub-native ticket graph snapshot."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from ..config import GitHubTicketGraphConfig
from ..domain.tickets import TicketGraphSnapshot


class TicketGraphGateway(Protocol):
    def read(
        self,
        cwd: Path,
        source: GitHubTicketGraphConfig,
        *,
        max_items: int,
    ) -> TicketGraphSnapshot: ...
