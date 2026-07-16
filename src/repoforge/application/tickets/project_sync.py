"""Deterministic planning and orchestration for GitHub ticket-graph synchronization."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ...domain.errors import ConfigError
from ...domain.ticket_sync import (
    MANAGED_FIELDS,
    MANAGED_VIEWS,
    ManagedFieldDefinition,
    TicketProjectOwnerType,
    TicketProjectPreflight,
    TicketProjectSnapshot,
    TicketProjectTarget,
    TicketSyncChange,
    TicketSyncChangeKind,
    TicketSyncConflict,
    TicketSyncPlan,
)
from ...domain.tickets import TicketGraph, TicketNode
from ..repository.issue_graph import read_github_ticket_snapshot

if TYPE_CHECKING:
    from ..context import ApplicationContext


@dataclass(frozen=True, slots=True)
class TicketProjectSyncCommand:
    repo_id: str
    owner: str
    project_number: int
    owner_type: TicketProjectOwnerType = TicketProjectOwnerType.ORGANIZATION
    apply: bool = False
    idempotency_key: str | None = None

    @property
    def target(self) -> TicketProjectTarget:
        return TicketProjectTarget(self.owner, self.project_number, self.owner_type)


@dataclass(frozen=True, slots=True)
class TicketProjectSyncFailure:
    change_id: str
    error_code: str
    message: str


@dataclass(frozen=True, slots=True)
class TicketProjectSyncResult:
    status: str
    mode: str
    target: TicketProjectTarget
    preflight: TicketProjectPreflight
    plan: TicketSyncPlan
    completed_change_ids: tuple[str, ...]
    pending_change_ids: tuple[str, ...]
    failed: TicketProjectSyncFailure | None
    manual_actions: tuple[str, ...]


def _field_values(node: TicketNode, sequence: int) -> dict[str, str]:
    return {
        "Type": node.ticket_type.value,
        "Priority": node.priority.value,
        "Status": node.status.value,
        "Parent / Initiative": str(node.parent or ""),
        "Sequence": str(sequence),
        "Roadmap phase": node.roadmap[0] if node.roadmap else "",
    }


def _field_conflict(
    definition: ManagedFieldDefinition, snapshot: TicketProjectSnapshot
) -> TicketSyncConflict | None:
    existing = snapshot.fields.get(definition.name)
    if existing is None:
        return None
    if existing.data_type != definition.data_type:
        return TicketSyncConflict(
            "MANAGED_FIELD_TYPE_DRIFT",
            f"field:{definition.name}",
            (
                f"Managed field {definition.name!r} has type {existing.data_type!r}; "
                f"expected {definition.data_type!r}."
            ),
        )
    missing_options = tuple(
        option for option in definition.options if option not in existing.options
    )
    if missing_options:
        return TicketSyncConflict(
            "MANAGED_FIELD_OPTIONS_DRIFT",
            f"field:{definition.name}",
            f"Managed field {definition.name!r} is missing options: {', '.join(missing_options)}.",
        )
    return None


def plan_ticket_project_sync(graph: TicketGraph, snapshot: TicketProjectSnapshot) -> TicketSyncPlan:
    """Return an additive, stable reconciliation plan without mutating GitHub.

    Existing unmanaged fields, views, project items, and relationships are intentionally ignored.
    Existing managed objects with incompatible shapes are reported as conflicts rather than changed.
    """

    changes: list[TicketSyncChange] = []
    conflicts: list[TicketSyncConflict] = []
    usable_fields: set[str] = set()

    for definition in MANAGED_FIELDS:
        conflict = _field_conflict(definition, snapshot)
        if conflict is not None:
            conflicts.append(conflict)
            continue
        usable_fields.add(definition.name)
        if definition.name not in snapshot.fields:
            changes.append(
                TicketSyncChange.create(
                    TicketSyncChangeKind.CREATE_FIELD,
                    {
                        "name": definition.name,
                        "data_type": definition.data_type,
                        "options": list(definition.options),
                    },
                )
            )

    node_by_number = {node.number: node for node in graph.nodes}
    usable_issues: set[int] = set()
    for node in graph.nodes:
        identity = snapshot.issue_identities.get(node.number)
        if identity is None:
            conflicts.append(
                TicketSyncConflict(
                    "ISSUE_IDENTITY_MISSING",
                    f"issue:{node.number}",
                    f"GitHub identity for issue #{node.number} is unavailable.",
                )
            )
            continue
        usable_issues.add(node.number)
        if node.number not in snapshot.items:
            changes.append(
                TicketSyncChange.create(
                    TicketSyncChangeKind.ADD_PROJECT_ITEM,
                    {
                        "issue": node.number,
                        "issue_node_id": identity.node_id,
                    },
                )
            )

    for sequence, node in enumerate(graph.nodes, start=1):
        if node.number not in usable_issues:
            continue
        current = snapshot.items.get(node.number)
        existing_values = current.field_values if current is not None else {}
        for definition in MANAGED_FIELDS:
            if definition.name not in usable_fields:
                continue
            desired = _field_values(node, sequence)[definition.name]
            if existing_values.get(definition.name) == desired:
                continue
            changes.append(
                TicketSyncChange.create(
                    TicketSyncChangeKind.SET_FIELD_VALUE,
                    {
                        "issue": node.number,
                        "field": definition.name,
                        "value": desired,
                    },
                )
            )

    desired_sub_issues = sorted(
        (node.parent, node.number)
        for node in graph.nodes
        if node.parent is not None and node.parent in node_by_number
    )
    for parent, child in desired_sub_issues:
        if (parent, child) in snapshot.sub_issues:
            continue
        parent_identity = snapshot.issue_identities.get(parent)
        child_identity = snapshot.issue_identities.get(child)
        if parent_identity is None or child_identity is None:
            continue
        changes.append(
            TicketSyncChange.create(
                TicketSyncChangeKind.ADD_SUB_ISSUE,
                {
                    "parent": parent,
                    "child": child,
                    "parent_issue_id": parent_identity.database_id,
                    "child_issue_id": child_identity.database_id,
                },
            )
        )

    desired_blocked_by = sorted(
        (node.number, blocker) for node in graph.nodes for blocker in node.blockers
    )
    for issue, blocker in desired_blocked_by:
        if (issue, blocker) in snapshot.blocked_by:
            continue
        issue_identity = snapshot.issue_identities.get(issue)
        blocker_identity = snapshot.issue_identities.get(blocker)
        if issue_identity is None or blocker_identity is None:
            continue
        changes.append(
            TicketSyncChange.create(
                TicketSyncChangeKind.ADD_BLOCKED_BY,
                {
                    "issue": issue,
                    "blocker": blocker,
                    "issue_id": issue_identity.database_id,
                    "blocker_issue_id": blocker_identity.database_id,
                },
            )
        )

    for view_definition in MANAGED_VIEWS:
        existing = snapshot.views.get(view_definition.name)
        if existing is None:
            changes.append(
                TicketSyncChange.create(
                    TicketSyncChangeKind.CREATE_VIEW,
                    {
                        "name": view_definition.name,
                        "layout": view_definition.layout,
                        "filter_query": view_definition.filter_query,
                        "sort_by": [list(item) for item in view_definition.sort_by],
                    },
                )
            )
            continue
        if (
            existing.layout != view_definition.layout
            or existing.filter_query != view_definition.filter_query
            or existing.sort_by != view_definition.sort_by
        ):
            conflicts.append(
                TicketSyncConflict(
                    "MANAGED_VIEW_DRIFT",
                    f"view:{view_definition.name}",
                    (
                        f"Managed view {view_definition.name!r} differs from the "
                        "checked-in definition."
                    ),
                )
            )

    return TicketSyncPlan(
        tuple(changes),
        tuple(conflicts),
        snapshot_incomplete=snapshot.identities_truncated or snapshot.items_truncated,
    )


GraphLoader = Callable[[Path], TicketGraph | None]


class TicketProjectSyncer:
    """Coordinate preflight, planning, and resumable per-change application."""

    def __init__(
        self,
        ctx: ApplicationContext,
        *,
        graph_loader: GraphLoader | None = None,
    ) -> None:
        self.ctx = ctx
        self.graph_loader = graph_loader

    @staticmethod
    def _manual_actions(result: dict[str, Any]) -> tuple[str, ...]:
        raw = result.get("manual_actions")
        if not isinstance(raw, (list, tuple)):
            return ()
        return tuple(item for item in raw if isinstance(item, str) and item)

    def execute(self, command: TicketProjectSyncCommand) -> TicketProjectSyncResult:
        target = command.target
        repo = self.ctx.repo(command.repo_id)
        gateway = self.ctx.ticket_projects
        if gateway is None:
            raise ConfigError("GitHub ticket-project synchronization is not configured")
        mode = "apply" if command.apply else "dry-run"
        details = {
            "repo_id": command.repo_id,
            "owner": target.owner,
            "project_number": target.project_number,
            "owner_type": target.owner_type.value,
            "mode": mode,
        }

        def run() -> TicketProjectSyncResult:
            if command.apply:
                raise ConfigError(
                    "ticket_project_sync apply is retired: edit issues, native sub-issues, "
                    "blocked-by relationships, and Project fields directly in GitHub"
                )
            preflight = gateway.preflight(repo.path, target, apply=False)
            empty_plan = TicketSyncPlan((), ())
            if not preflight.ready:
                return TicketProjectSyncResult(
                    "preflight_failed",
                    mode,
                    target,
                    preflight,
                    empty_plan,
                    (),
                    (),
                    None,
                    (),
                )
            if self.graph_loader is not None:
                graph = self.graph_loader(repo.path)
                if graph is None:
                    raise ConfigError("Ticket graph fixture is unavailable")
            else:
                graph_snapshot, _ = read_github_ticket_snapshot(
                    self.ctx,
                    repo,
                    root_issue=None,
                    fresh=True,
                )
                graph = graph_snapshot.graph
            snapshot = gateway.snapshot(repo.path, target, graph)
            plan = plan_ticket_project_sync(graph, snapshot)
            pending = tuple(change.change_id for change in plan.changes)
            return TicketProjectSyncResult(
                "noop" if not plan.changes and not plan.conflicts else "planned",
                mode,
                target,
                preflight,
                plan,
                (),
                pending,
                None,
                (
                    ("GitHub is authoritative; make any reported repairs directly in GitHub.",)
                    if plan.changes or plan.conflicts
                    else ()
                ),
            )

        return self.ctx.audited(
            "ticket_project_sync_apply" if command.apply else "ticket_project_sync_plan",
            details,
            run,
            mutating=command.apply,
        )
