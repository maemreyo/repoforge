"""Constrained GitHub Project and native issue-relationship boundary."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

from ..domain.ticket_sync import (
    TicketProjectPreflight,
    TicketProjectSnapshot,
    TicketProjectTarget,
    TicketSyncChange,
)
from ..domain.tickets import TicketGraph


class TicketProjectGateway(Protocol):
    def preflight(
        self,
        cwd: Path,
        target: TicketProjectTarget,
        *,
        apply: bool,
    ) -> TicketProjectPreflight: ...

    def snapshot(
        self,
        cwd: Path,
        target: TicketProjectTarget,
        graph: TicketGraph,
    ) -> TicketProjectSnapshot: ...

    def apply_change(
        self,
        cwd: Path,
        target: TicketProjectTarget,
        change: TicketSyncChange,
    ) -> dict[str, Any]: ...
