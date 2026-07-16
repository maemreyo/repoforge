from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from repoforge.application.tickets.project_sync import (
    TicketProjectSyncCommand,
    TicketProjectSyncer,
    plan_ticket_project_sync,
)
from repoforge.domain.errors import ConfigError
from repoforge.domain.ticket_sync import (
    MANAGED_FIELDS,
    MANAGED_VIEWS,
    TicketIssueIdentity,
    TicketProjectFieldSnapshot,
    TicketProjectItemSnapshot,
    TicketProjectOwnerType,
    TicketProjectPreflight,
    TicketProjectSnapshot,
    TicketProjectTarget,
    TicketProjectViewSnapshot,
    TicketSyncChange,
    TicketSyncChangeKind,
)
from repoforge.domain.tickets import (
    TicketGraph,
    TicketNode,
    TicketPriority,
    TicketStatus,
    TicketType,
)


def _graph() -> TicketGraph:
    return TicketGraph(
        schema_version=1,
        program_issue=1,
        nodes=(
            TicketNode(
                1,
                "Program",
                TicketType.PROGRAM,
                TicketPriority.P0,
                TicketStatus.IN_PROGRESS,
                None,
                (),
                (),
                (2,),
                ("master",),
            ),
            TicketNode(
                2,
                "Initiative",
                TicketType.INITIATIVE,
                TicketPriority.P1,
                TicketStatus.READY,
                1,
                (),
                (),
                (3, 4),
                ("master",),
            ),
            TicketNode(
                3,
                "Implementation",
                TicketType.IMPLEMENTATION_TICKET,
                TicketPriority.P1,
                TicketStatus.BLOCKED,
                2,
                (4,),
                (),
                (),
                ("master",),
            ),
            TicketNode(
                4,
                "Prerequisite",
                TicketType.IMPLEMENTATION_TICKET,
                TicketPriority.P0,
                TicketStatus.READY,
                2,
                (),
                (3,),
                (),
                ("master",),
            ),
        ),
    )


def _empty_snapshot() -> TicketProjectSnapshot:
    return TicketProjectSnapshot(
        project_id="PVT_project",
        project_title="RepoForge Delivery",
        fields={},
        items={},
        views={},
        issue_identities={
            number: TicketIssueIdentity(number, f"ISSUE_{number}", 1000 + number)
            for number in range(1, 5)
        },
        sub_issues=frozenset(),
        blocked_by=frozenset(),
    )


def _preflight(*, ready: bool = True) -> TicketProjectPreflight:
    return TicketProjectPreflight(
        authenticated=True,
        ready=ready,
        scopes=("project", "repo"),
        missing_scopes=() if ready else ("project",),
        rate_remaining=4999,
        rate_reset="2026-07-16T00:00:00Z",
        warnings=(),
    )


def test_managed_contract_is_explicit_and_stable() -> None:
    assert [field.name for field in MANAGED_FIELDS] == [
        "Type",
        "Priority",
        "Status",
        "Parent / Initiative",
        "Sequence",
        "Roadmap phase",
    ]
    assert [view.name for view in MANAGED_VIEWS] == [
        "Ready Queue",
        "By Initiative",
        "Blocked",
        "Roadmap",
        "In Review",
        "Done",
    ]
    ready = MANAGED_VIEWS[0]
    assert ready.filter_query == "Status:Ready"
    assert ready.sort_by == (("Priority", "asc"), ("Sequence", "asc"))


def test_project_target_rejects_unbounded_or_invalid_identity() -> None:
    target = TicketProjectTarget("maemreyo", 7, TicketProjectOwnerType.USER)
    assert target.owner == "maemreyo"

    for owner in ("", "bad owner", "../escape"):
        try:
            TicketProjectTarget(owner, 7, TicketProjectOwnerType.USER)
        except ValueError:
            pass
        else:  # pragma: no cover - assertion branch
            raise AssertionError(f"owner {owner!r} should be rejected")

    try:
        TicketProjectTarget("maemreyo", 0, TicketProjectOwnerType.USER)
    except ValueError:
        pass
    else:  # pragma: no cover - assertion branch
        raise AssertionError("non-positive project numbers must be rejected")


def test_planner_emits_deterministic_non_destructive_changes() -> None:
    first = plan_ticket_project_sync(_graph(), _empty_snapshot())
    second = plan_ticket_project_sync(_graph(), _empty_snapshot())

    assert first == second
    assert first.conflicts == ()
    assert len({change.change_id for change in first.changes}) == len(first.changes)
    assert [change.kind for change in first.changes[:6]] == [
        TicketSyncChangeKind.CREATE_FIELD,
    ] * 6
    assert all(not change.kind.value.startswith("remove") for change in first.changes)
    assert all(not change.kind.value.startswith("delete") for change in first.changes)
    assert any(change.kind is TicketSyncChangeKind.ADD_SUB_ISSUE for change in first.changes)
    assert any(change.kind is TicketSyncChangeKind.ADD_BLOCKED_BY for change in first.changes)
    assert [
        change.payload["name"]
        for change in first.changes
        if change.kind is TicketSyncChangeKind.CREATE_VIEW
    ] == [view.name for view in MANAGED_VIEWS]


def test_planner_preserves_unmanaged_state_and_reports_managed_drift() -> None:
    snapshot = _empty_snapshot()
    snapshot = TicketProjectSnapshot(
        project_id=snapshot.project_id,
        project_title=snapshot.project_title,
        fields={
            "Custom": TicketProjectFieldSnapshot("FIELD_custom", "TEXT", {}),
            "Priority": TicketProjectFieldSnapshot("FIELD_priority", "TEXT", {}),
        },
        items=snapshot.items,
        views={
            "My View": TicketProjectViewSnapshot("VIEW_custom", "table", "is:issue", ()),
            "Ready Queue": TicketProjectViewSnapshot("VIEW_ready", "board", "Status:Backlog", ()),
        },
        issue_identities=snapshot.issue_identities,
        sub_issues=frozenset({(99, 98)}),
        blocked_by=frozenset({(97, 96)}),
    )

    plan = plan_ticket_project_sync(_graph(), snapshot)

    assert {conflict.subject for conflict in plan.conflicts} == {
        "field:Priority",
        "view:Ready Queue",
    }
    assert all(change.payload.get("name") != "Custom" for change in plan.changes)
    assert all(change.payload.get("name") != "My View" for change in plan.changes)
    assert all(change.payload.get("parent") != 99 for change in plan.changes)
    assert all(change.payload.get("issue") != 97 for change in plan.changes)


def test_planner_flags_snapshot_incomplete_instead_of_asserting_confirmed_drift() -> None:
    """A truncated snapshot must not be indistinguishable from a fully-observed one that
    happens to be missing an identity/item: the planner still proposes the same additive
    changes (dropping them silently would hide real work), but it must also surface
    `snapshot_incomplete` so a caller does not read them as confirmed drift."""
    base = _empty_snapshot()
    truncated = TicketProjectSnapshot(
        project_id=base.project_id,
        project_title=base.project_title,
        fields=base.fields,
        items=base.items,
        views=base.views,
        issue_identities=base.issue_identities,
        sub_issues=base.sub_issues,
        blocked_by=base.blocked_by,
        identities_truncated=True,
    )

    complete_plan = plan_ticket_project_sync(_graph(), base)
    truncated_plan = plan_ticket_project_sync(_graph(), truncated)

    assert complete_plan.snapshot_incomplete is False
    assert truncated_plan.snapshot_incomplete is True
    assert truncated_plan.changes == complete_plan.changes
    assert truncated_plan.conflicts == complete_plan.conflicts


def test_planner_is_noop_when_managed_projection_matches() -> None:
    graph = _graph()
    initial = plan_ticket_project_sync(graph, _empty_snapshot())
    fields = {
        definition.name: TicketProjectFieldSnapshot(
            f"FIELD_{index}",
            definition.data_type,
            {option: f"OPTION_{index}_{option}" for option in definition.options},
        )
        for index, definition in enumerate(MANAGED_FIELDS)
    }
    items = {
        node.number: TicketProjectItemSnapshot(
            f"ITEM_{node.number}",
            {
                "Type": node.ticket_type.value,
                "Priority": node.priority.value,
                "Status": node.status.value,
                "Parent / Initiative": str(node.parent or ""),
                "Sequence": str(index + 1),
                "Roadmap phase": node.roadmap[0],
            },
        )
        for index, node in enumerate(graph.nodes)
    }
    views = {
        view.name: TicketProjectViewSnapshot(
            f"VIEW_{index}", view.layout, view.filter_query, view.sort_by
        )
        for index, view in enumerate(MANAGED_VIEWS)
    }
    snapshot = TicketProjectSnapshot(
        project_id="PVT_project",
        project_title="RepoForge Delivery",
        fields=fields,
        items=items,
        views=views,
        issue_identities=_empty_snapshot().issue_identities,
        sub_issues=frozenset({(1, 2), (2, 3), (2, 4)}),
        blocked_by=frozenset({(3, 4)}),
    )

    plan = plan_ticket_project_sync(graph, snapshot)

    assert initial.changes
    assert plan.changes == ()
    assert plan.conflicts == ()


class _Gateway:
    def __init__(self, *, ready: bool = True, fail_once_at: int | None = None) -> None:
        self.preflight_result = _preflight(ready=ready)
        self.snapshot_calls = 0
        self.apply_calls: list[str] = []
        self._fail_once_at = fail_once_at
        self._failed = False

    def preflight(
        self, cwd: Path, target: TicketProjectTarget, *, apply: bool
    ) -> TicketProjectPreflight:
        del cwd, target, apply
        return self.preflight_result

    def snapshot(
        self, cwd: Path, target: TicketProjectTarget, graph: TicketGraph
    ) -> TicketProjectSnapshot:
        del cwd, target, graph
        self.snapshot_calls += 1
        return _empty_snapshot()

    def apply_change(
        self,
        cwd: Path,
        target: TicketProjectTarget,
        change: TicketSyncChange,
    ) -> dict[str, Any]:
        del cwd, target
        call_index = len(self.apply_calls)
        if self._fail_once_at == call_index and not self._failed:
            self._failed = True
            raise RuntimeError("injected GitHub failure")
        self.apply_calls.append(change.change_id)
        return {
            "status": "applied",
            "manual_actions": (
                ["Configure view sorting in GitHub."]
                if change.kind is TicketSyncChangeKind.CREATE_VIEW and change.payload.get("sort_by")
                else []
            ),
        }


class _Context:
    def __init__(self, root: Path, gateway: _Gateway) -> None:
        self.root = root
        self.ticket_projects = gateway
        self.audit_calls: list[tuple[str, bool | None]] = []
        self.idempotency_calls: list[tuple[str, str | None]] = []
        self._results: dict[tuple[str, str], Any] = {}

    def repo(self, repo_id: str) -> Any:
        assert repo_id == "repoforge"
        return SimpleNamespace(path=self.root)

    def audited(
        self,
        action: str,
        details: dict[str, Any],
        operation: Any,
        *,
        mutating: bool | None = None,
        correlation_id: str | None = None,
    ) -> Any:
        del details, correlation_id
        self.audit_calls.append((action, mutating))
        return operation()

    def idempotent(
        self,
        action: str,
        key: str | None,
        request: Any,
        operation: Any,
        **kwargs: Any,
    ) -> Any:
        del request, kwargs
        self.idempotency_calls.append((action, key))
        assert key is not None
        identity = (action, key)
        if identity in self._results:
            return self._results[identity]
        result = operation()
        self._results[identity] = result
        return result


def _syncer(tmp_path: Path, gateway: _Gateway) -> TicketProjectSyncer:
    return TicketProjectSyncer(
        _Context(tmp_path, gateway),
        graph_loader=lambda path: _graph(),
    )


def _command(*, apply: bool) -> TicketProjectSyncCommand:
    return TicketProjectSyncCommand(
        repo_id="repoforge",
        owner="maemreyo",
        project_number=7,
        owner_type=TicketProjectOwnerType.USER,
        apply=apply,
        idempotency_key="issue-63-sync",
    )


def test_sync_dry_run_never_invokes_mutations(tmp_path: Path) -> None:
    gateway = _Gateway()
    syncer = _syncer(tmp_path, gateway)

    result = syncer.execute(_command(apply=False))

    assert result.status == "planned"
    assert result.mode == "dry-run"
    assert result.completed_change_ids == ()
    assert result.pending_change_ids
    assert gateway.snapshot_calls == 1
    assert gateway.apply_calls == []
    assert syncer.ctx.audit_calls == [("ticket_project_sync_plan", False)]


def test_sync_apply_is_retired_before_any_github_call(tmp_path: Path) -> None:
    gateway = _Gateway(ready=False)
    syncer = _syncer(tmp_path, gateway)

    with pytest.raises(ConfigError, match="apply is retired"):
        syncer.execute(_command(apply=True))

    assert gateway.snapshot_calls == 0
    assert gateway.apply_calls == []
    assert syncer.ctx.audit_calls == [("ticket_project_sync_apply", True)]
