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
    SUPERSEDED = "Superseded"


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


@dataclass(frozen=True, slots=True)
class TicketDeliveryMetadata:
    specification_complete: bool
    unresolved_design_gate: bool = False
    superseded_by: int | None = None
    wave: int = 0
    sequence: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.specification_complete, bool):
            raise TicketGraphError("specification_complete must be a boolean")
        if not isinstance(self.unresolved_design_gate, bool):
            raise TicketGraphError("unresolved_design_gate must be a boolean")
        if self.superseded_by is not None and (
            not isinstance(self.superseded_by, int)
            or isinstance(self.superseded_by, bool)
            or self.superseded_by <= 0
        ):
            raise TicketGraphError("superseded_by must be a positive issue number")
        for field, value in (("wave", self.wave), ("sequence", self.sequence)):
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise TicketGraphError(f"{field} must be a non-negative integer")


@dataclass(frozen=True, slots=True)
class TicketLiveState:
    number: int
    is_open: bool | None
    delivery: TicketDeliveryMetadata

    def __post_init__(self) -> None:
        if not isinstance(self.number, int) or isinstance(self.number, bool) or self.number <= 0:
            raise TicketGraphError("live ticket number must be a positive integer")
        if self.is_open is not None and not isinstance(self.is_open, bool):
            raise TicketGraphError("is_open must be a boolean or None")


@dataclass(frozen=True, slots=True)
class TicketReadinessPolicy:
    p0_limit: int = 2
    p1_limit: int = 3
    p2_limit: int = 4
    p3_limit: int = 4
    initiative_limit: int = 2

    def __post_init__(self) -> None:
        for field, value in (
            ("p0_limit", self.p0_limit),
            ("p1_limit", self.p1_limit),
            ("p2_limit", self.p2_limit),
            ("p3_limit", self.p3_limit),
            ("initiative_limit", self.initiative_limit),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise TicketGraphError(f"{field} must be a positive integer")

    @classmethod
    def unbounded(cls) -> TicketReadinessPolicy:
        return cls(2_000, 2_000, 2_000, 2_000, 2_000)

    def priority_limit(self, priority: TicketPriority) -> int:
        return {
            TicketPriority.P0: self.p0_limit,
            TicketPriority.P1: self.p1_limit,
            TicketPriority.P2: self.p2_limit,
            TicketPriority.P3: self.p3_limit,
        }[priority]


@dataclass(frozen=True, slots=True)
class TicketReadinessAssessment:
    number: int
    declared_status: TicketStatus
    derived_status: TicketStatus
    selectable: bool
    reason_codes: tuple[str, ...]
    reasons: tuple[str, ...]
    unresolved_blockers: tuple[int, ...]
    wip_conflicts: tuple[int, ...]
    metadata_repairs: tuple[str, ...]
    wave: int
    sequence: int


@dataclass(frozen=True, slots=True)
class TicketReadinessReport:
    assessments: tuple[TicketReadinessAssessment, ...]
    recommended: tuple[int, ...]
    diagnostics: tuple[TicketDiagnostic, ...]


class TicketGraphError(ValueError):
    """Raised when a graph cannot be parsed or a bounded operation is invalid."""
