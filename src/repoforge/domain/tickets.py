"""Typed GitHub-native ticket graph contracts."""

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


class RequirementRelationType(str, Enum):
    SUPERSEDES = "supersedes"
    SUPERSEDED_BY = "superseded_by"
    SPLIT_INTO = "split_into"
    MERGED_INTO = "merged_into"
    INVALIDATES = "invalidates"


@dataclass(frozen=True, slots=True, order=True)
class RequirementRelation:
    relation_type: RequirementRelationType
    target_issue: int
    reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.relation_type, RequirementRelationType):
            raise TicketGraphError("relation_type must be a RequirementRelationType")
        if (
            not isinstance(self.target_issue, int)
            or isinstance(self.target_issue, bool)
            or self.target_issue <= 0
        ):
            raise TicketGraphError("relation target must be a positive issue number")
        if not isinstance(self.reason, str) or not self.reason.strip() or len(self.reason) > 500:
            raise TicketGraphError("relation reason must be a non-empty bounded string")


@dataclass(frozen=True, slots=True)
class PartialCompletion:
    verified_deliverables: tuple[str, ...]
    remaining_scope: tuple[str, ...]
    new_child_issues: tuple[int, ...]
    unverified_work: tuple[str, ...]
    handoff_notes: tuple[str, ...]
    rejected_scope: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for field_name, values in (
            ("verified_deliverables", self.verified_deliverables),
            ("remaining_scope", self.remaining_scope),
            ("unverified_work", self.unverified_work),
            ("handoff_notes", self.handoff_notes),
            ("rejected_scope", self.rejected_scope),
        ):
            if not isinstance(values, tuple) or len(values) > 64:
                raise TicketGraphError(f"{field_name} must be a bounded tuple")
            if any(
                not isinstance(item, str) or not item.strip() or len(item) > 500 for item in values
            ):
                raise TicketGraphError(f"{field_name} contains an invalid item")
        if not isinstance(self.new_child_issues, tuple) or len(self.new_child_issues) > 64:
            raise TicketGraphError("new_child_issues must be a bounded tuple")
        if any(
            not isinstance(item, int) or isinstance(item, bool) or item <= 0
            for item in self.new_child_issues
        ):
            raise TicketGraphError("new_child_issues must contain positive issue numbers")
        if tuple(sorted(set(self.new_child_issues))) != self.new_child_issues:
            raise TicketGraphError("new_child_issues must be sorted and unique")

    @property
    def has_remaining_scope(self) -> bool:
        return bool(self.remaining_scope or self.new_child_issues or self.unverified_work)


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
    comments: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class TicketGraphSnapshot:
    """One bounded, internally consistent observation of the GitHub ticket graph."""

    graph: TicketGraph
    observed_at: str
    evidence_complete: bool
    unavailable: tuple[int, ...]
    truncated: bool
    live_issues: tuple[TicketLiveMetadata, ...] = ()


@dataclass(frozen=True, slots=True)
class TicketDeliveryMetadata:
    specification_complete: bool
    unresolved_design_gate: bool = False
    superseded_by: int | None = None
    relations: tuple[RequirementRelation, ...] = ()
    partial_completion: PartialCompletion | None = None
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
        if not isinstance(self.relations, tuple) or len(self.relations) > 128:
            raise TicketGraphError("relations must be a bounded tuple")
        if any(not isinstance(item, RequirementRelation) for item in self.relations):
            raise TicketGraphError("relations must contain RequirementRelation values")
        relation_order = {value: index for index, value in enumerate(RequirementRelationType)}
        relation_keys = tuple(
            (relation_order[item.relation_type], item.target_issue) for item in self.relations
        )
        if tuple(sorted(set(relation_keys))) != relation_keys:
            raise TicketGraphError("relations must be sorted and unique by type and target")
        if self.partial_completion is not None and not isinstance(
            self.partial_completion, PartialCompletion
        ):
            raise TicketGraphError("partial_completion must be PartialCompletion or None")
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
