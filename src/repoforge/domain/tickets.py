"""Typed roadmap ticket graph contracts."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class TicketType(str, Enum):
    PROGRAM = "program"
    INITIATIVE = "initiative"
    IMPLEMENTATION_TICKET = "implementation_ticket"


class TicketPriority(str, Enum):
    P0 = "P0"
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"


class TicketStatus(str, Enum):
    BACKLOG = "Backlog"
    READY = "Ready"
    IN_PROGRESS = "In progress"
    BLOCKED = "Blocked"
    IN_REVIEW = "In review"
    DONE = "Done"


@dataclass(frozen=True, slots=True)
class TicketNode:
    number: int
    title: str
    ticket_type: TicketType
    priority: TicketPriority
    status: TicketStatus
    parent: int | None
    blockers: tuple[int, ...]
    blocks: tuple[int, ...]
    children: tuple[int, ...]
    roadmap: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TicketGraph:
    schema_version: int
    program_issue: int
    nodes: tuple[TicketNode, ...]


@dataclass(frozen=True, slots=True, order=True)
class TicketDiagnostic:
    code: str
    issue_number: int
    message: str


@dataclass(frozen=True, slots=True)
class TicketLiveMetadata:
    number: int
    title: str
    state: str
    body: str


class TicketGraphError(ValueError):
    """Raised when a graph cannot be parsed or a bounded operation is invalid."""
