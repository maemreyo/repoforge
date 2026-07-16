"""Pure contracts for deterministic GitHub Project ticket-graph synchronization."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any

from .tickets import TicketPriority, TicketStatus, TicketType

_OWNER = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,98}[A-Za-z0-9])?$")


class TicketProjectOwnerType(str, Enum):
    ORGANIZATION = "organization"
    USER = "user"


@dataclass(frozen=True, slots=True)
class TicketProjectTarget:
    owner: str
    project_number: int
    owner_type: TicketProjectOwnerType = TicketProjectOwnerType.ORGANIZATION

    def __post_init__(self) -> None:
        if not _OWNER.fullmatch(self.owner):
            raise ValueError("GitHub project owner must be a bounded login or organization name")
        if self.project_number <= 0:
            raise ValueError("GitHub project number must be positive")


@dataclass(frozen=True, slots=True)
class TicketProjectPreflight:
    authenticated: bool
    ready: bool
    scopes: tuple[str, ...]
    missing_scopes: tuple[str, ...]
    rate_remaining: int | None
    rate_reset: str | None
    warnings: tuple[str, ...]


class TicketSyncChangeKind(str, Enum):
    """The only additive or managed-value mutations the sync workflow may perform."""

    CREATE_FIELD = "create_field"
    ADD_PROJECT_ITEM = "add_project_item"
    SET_FIELD_VALUE = "set_field_value"
    ADD_SUB_ISSUE = "add_sub_issue"
    ADD_BLOCKED_BY = "add_blocked_by"
    CREATE_VIEW = "create_view"


@dataclass(frozen=True, slots=True)
class ManagedFieldDefinition:
    name: str
    data_type: str
    options: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ManagedViewDefinition:
    name: str
    layout: str
    filter_query: str
    sort_by: tuple[tuple[str, str], ...] = ()


MANAGED_FIELDS: tuple[ManagedFieldDefinition, ...] = (
    ManagedFieldDefinition("Type", "SINGLE_SELECT", tuple(item.value for item in TicketType)),
    ManagedFieldDefinition(
        "Priority", "SINGLE_SELECT", tuple(item.value for item in TicketPriority)
    ),
    ManagedFieldDefinition("Status", "SINGLE_SELECT", tuple(item.value for item in TicketStatus)),
    ManagedFieldDefinition("Parent / Initiative", "TEXT"),
    ManagedFieldDefinition("Sequence", "NUMBER"),
    ManagedFieldDefinition("Roadmap phase", "TEXT"),
)

MANAGED_VIEWS: tuple[ManagedViewDefinition, ...] = (
    ManagedViewDefinition(
        "Ready Queue",
        "table",
        "Status:Ready",
        (("Priority", "asc"), ("Sequence", "asc")),
    ),
    ManagedViewDefinition("By Initiative", "table", "", (("Parent / Initiative", "asc"),)),
    ManagedViewDefinition("Blocked", "table", "Status:Blocked"),
    ManagedViewDefinition("Roadmap", "roadmap", ""),
    ManagedViewDefinition("In Review", "table", 'Status:"In review"'),
    ManagedViewDefinition("Done", "table", "Status:Done"),
)


@dataclass(frozen=True, slots=True)
class TicketIssueIdentity:
    number: int
    node_id: str
    database_id: int


@dataclass(frozen=True, slots=True)
class TicketProjectFieldSnapshot:
    field_id: str
    data_type: str
    options: dict[str, str]


@dataclass(frozen=True, slots=True)
class TicketProjectItemSnapshot:
    item_id: str
    field_values: dict[str, str]


@dataclass(frozen=True, slots=True)
class TicketProjectViewSnapshot:
    view_id: str
    layout: str
    filter_query: str
    sort_by: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class TicketProjectSnapshot:
    project_id: str
    project_title: str
    fields: dict[str, TicketProjectFieldSnapshot]
    items: dict[int, TicketProjectItemSnapshot]
    views: dict[str, TicketProjectViewSnapshot]
    issue_identities: dict[int, TicketIssueIdentity]
    sub_issues: frozenset[tuple[int, int]]
    blocked_by: frozenset[tuple[int, int]]


@dataclass(frozen=True, slots=True)
class TicketSyncChange:
    change_id: str
    kind: TicketSyncChangeKind
    payload: dict[str, Any]

    @classmethod
    def create(cls, kind: TicketSyncChangeKind, payload: dict[str, Any]) -> TicketSyncChange:
        canonical = json.dumps(
            {"kind": kind.value, "payload": payload},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        return cls(hashlib.sha256(canonical).hexdigest(), kind, payload)


@dataclass(frozen=True, slots=True, order=True)
class TicketSyncConflict:
    code: str
    subject: str
    message: str


@dataclass(frozen=True, slots=True)
class TicketSyncPlan:
    changes: tuple[TicketSyncChange, ...]
    conflicts: tuple[TicketSyncConflict, ...]
