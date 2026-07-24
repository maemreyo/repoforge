from __future__ import annotations

from dataclasses import replace

import pytest

from repoforge.adapters.locking import FcntlLockManager
from repoforge.adapters.persistence.json_issue_graph_proposal_store import (
    JsonIssueGraphProposalStore,
)
from repoforge.application.repository.issue_graph_proposal import (
    IssueGraphProposalService,
)
from repoforge.domain.errors import ErrorCode, RepoForgeError
from repoforge.domain.issue_graph_proposal import (
    IssueEdgeDraft,
    IssueEdgeKind,
    IssueGraphDraft,
    IssueGraphIdentity,
    IssueNodeDraft,
    LiveIssueCandidate,
    plan_issue_graph,
    proposal_stale_fields,
)


def _identity(**changes: object) -> IssueGraphIdentity:
    values: dict[str, object] = {
        "repo_id": "repoforge",
        "repository_fingerprint": "1" * 64,
        "base_commit_sha": "2" * 40,
        "live_snapshot_sha256": "3" * 64,
        "active_generation": 12,
        "tool_surface_hash": "4" * 64,
        "input_contract_digest": "5" * 64,
        "output_contract_digest": "6" * 64,
        "template_version": 2,
        "schema_version": 1,
    }
    values.update(changes)
    return IssueGraphIdentity(**values)  # type: ignore[arg-type]


def _node(
    ref: str,
    *,
    title: str | None = None,
    parent_ref: str | None = None,
    status: str = "ready",
) -> IssueNodeDraft:
    return IssueNodeDraft(
        client_ref=ref,
        title=title or ref.replace("-", " ").title(),
        ticket_type="epic" if parent_ref is None else "task",
        priority="p0",
        status=status,
        parent_ref=parent_ref,
        body=(
            "## Objective\n\nDeliver "
            + ref
            + ".\n\n## Acceptance criteria\n\n- [ ] Deterministic evidence exists.\n"
        ),
    )


def _draft(*, reverse: bool = False) -> IssueGraphDraft:
    nodes = (
        _node("epic-232", title="Control-plane truth hardening"),
        _node("task-233", parent_ref="epic-232"),
        _node("task-234", parent_ref="epic-232"),
        _node("task-244", parent_ref="epic-232", status="planned"),
    )
    edges = (
        IssueEdgeDraft("task-234", "task-233", IssueEdgeKind.BLOCKED_BY),
        IssueEdgeDraft("task-244", "task-234", IssueEdgeKind.BLOCKED_BY),
    )
    return IssueGraphDraft(
        repo_id="repoforge",
        root_ref="epic-232",
        nodes=tuple(reversed(nodes)) if reverse else nodes,
        edges=tuple(reversed(edges)) if reverse else edges,
    )


def test_semantic_graph_is_byte_stable_and_plans_parent_before_dependencies() -> None:
    first = plan_issue_graph(
        _draft(),
        _identity(),
        live_issues=(),
        created_at="2026-07-22T00:00:00+00:00",
        expires_at="2026-07-23T00:00:00+00:00",
    )
    second = plan_issue_graph(
        _draft(reverse=True),
        _identity(),
        live_issues=(),
        created_at="2026-07-22T00:00:00+00:00",
        expires_at="2026-07-23T00:00:00+00:00",
    )

    assert first.canonical_json == second.canonical_json
    assert first.proposal_hash == second.proposal_hash
    assert first.publication_order == second.publication_order
    assert first.publication_order[:4] == (
        "node:epic-232",
        "node:task-233",
        "node:task-234",
        "node:task-244",
    )
    assert first.publication_order[4:] == (
        "parent:task-233->epic-232",
        "parent:task-234->epic-232",
        "parent:task-244->epic-232",
        "blocked_by:task-234->task-233",
        "blocked_by:task-244->task-234",
        "checklist:epic-232",
    )


@pytest.mark.parametrize(
    ("draft", "code"),
    [
        (
            IssueGraphDraft(
                "repoforge",
                "a",
                (_node("a"),),
                (IssueEdgeDraft("a", "missing", IssueEdgeKind.BLOCKED_BY),),
            ),
            "UNRESOLVED_REFERENCE",
        ),
        (
            IssueGraphDraft(
                "repoforge",
                "a",
                (_node("a"), _node("b", parent_ref="a")),
                (
                    IssueEdgeDraft("a", "b", IssueEdgeKind.BLOCKED_BY),
                    IssueEdgeDraft("b", "a", IssueEdgeKind.BLOCKED_BY),
                ),
            ),
            "GRAPH_CYCLE",
        ),
        (
            IssueGraphDraft(
                "repoforge",
                "a",
                (
                    _node("a"),
                    _node("b", parent_ref="a"),
                    _node("c", parent_ref="b"),
                    _node("d", parent_ref="c"),
                ),
                (),
            ),
            "HIERARCHY_DEPTH_UNSUPPORTED",
        ),
        (
            IssueGraphDraft(
                "repoforge",
                "a",
                (_node("a"), _node("b", parent_ref="a")),
                (
                    IssueEdgeDraft("b", "a", IssueEdgeKind.BLOCKED_BY),
                    IssueEdgeDraft("b", "a", IssueEdgeKind.BLOCKED_BY),
                ),
            ),
            "DUPLICATE_EDGE",
        ),
        (
            IssueGraphDraft(
                "repoforge",
                "a",
                (_node("a"), _node("b", parent_ref="unknown")),
                (),
            ),
            "UNKNOWN_PARENT",
        ),
    ],
)
def test_invalid_graphs_fail_before_a_proposal_exists(
    draft: IssueGraphDraft,
    code: str,
) -> None:
    with pytest.raises(RepoForgeError) as failure:
        plan_issue_graph(
            draft,
            _identity(),
            live_issues=(),
            created_at="2026-07-22T00:00:00+00:00",
            expires_at="2026-07-23T00:00:00+00:00",
        )

    assert failure.value.code is ErrorCode.PROPOSAL_BLOCKED
    assert code in {item["code"] for item in failure.value.details["findings"]}


def test_exact_managed_markers_are_authoritative_and_conflicts_fail_closed() -> None:
    draft = _draft()
    exact = LiveIssueCandidate(
        issue_number=232,
        title="An unrelated renamed title",
        managed_marker="<!-- repoforge-issue:epic-232 -->",
    )
    title_only = LiveIssueCandidate(
        issue_number=999,
        title="Task 233",
        managed_marker=None,
    )
    proposal = plan_issue_graph(
        draft,
        _identity(),
        live_issues=(title_only, exact),
        created_at="2026-07-22T00:00:00+00:00",
        expires_at="2026-07-23T00:00:00+00:00",
    )

    assert proposal.delta.exact_matches == (("epic-232", 232),)
    assert proposal.delta.duplicate_candidates == (("task-233", 999),)

    with pytest.raises(RepoForgeError) as duplicate:
        plan_issue_graph(
            draft,
            _identity(),
            live_issues=(
                exact,
                replace(exact, issue_number=233),
            ),
            created_at="2026-07-22T00:00:00+00:00",
            expires_at="2026-07-23T00:00:00+00:00",
        )
    assert duplicate.value.details["findings"][0]["code"] == "CONFLICTING_MANAGED_MARKER"


def test_any_bound_identity_change_makes_the_proposal_stale() -> None:
    proposal = plan_issue_graph(
        _draft(),
        _identity(),
        live_issues=(),
        created_at="2026-07-22T00:00:00+00:00",
        expires_at="2026-07-23T00:00:00+00:00",
    )

    fields = proposal_stale_fields(
        proposal,
        _identity(
            base_commit_sha="7" * 40,
            live_snapshot_sha256="8" * 64,
            active_generation=13,
            tool_surface_hash="9" * 64,
            template_version=3,
        ),
    )
    assert fields == (
        "base_commit_sha",
        "live_snapshot_sha256",
        "active_generation",
        "tool_surface_hash",
        "template_version",
    )


def test_private_store_is_immutable_and_preserves_expiry(tmp_path) -> None:
    locks = FcntlLockManager(tmp_path / "locks")
    store = JsonIssueGraphProposalStore(tmp_path / "state", locks)
    proposal = plan_issue_graph(
        _draft(),
        _identity(),
        live_issues=(),
        created_at="2026-07-22T00:00:00+00:00",
        expires_at="2026-07-23T00:00:00+00:00",
    )

    created = store.create(proposal)
    replay = store.create(proposal)
    loaded = store.read(proposal.proposal_id)

    assert replay == created
    assert loaded is not None
    assert loaded.value == proposal
    assert loaded.value.expires_at == "2026-07-23T00:00:00+00:00"

    conflicting = replace(proposal, canonical_json=proposal.canonical_json + " ")
    with pytest.raises(RepoForgeError) as collision:
        store.create(conflicting)
    assert collision.value.code is ErrorCode.ALREADY_EXISTS


def test_application_preview_is_effect_free_and_create_is_private_immutable_metadata(
    tmp_path,
) -> None:
    store = JsonIssueGraphProposalStore(
        tmp_path / "state",
        FcntlLockManager(tmp_path / "locks"),
    )
    service = IssueGraphProposalService(store)

    preview = service.preview(
        _draft(),
        _identity(),
        live_issues=(),
        created_at="2026-07-22T00:00:00+00:00",
        expires_at="2026-07-23T00:00:00+00:00",
    )
    assert store.list_records().records == ()
    assert preview.external_writes == 0

    created = service.create(preview)
    assert created.value == preview
    assert service.read(preview.proposal_id) == preview
    assert service.inspect(preview.proposal_id, _identity()) == ()
    assert service.inspect(
        preview.proposal_id,
        _identity(active_generation=13),
    ) == ("active_generation",)


def test_232_shaped_symbolic_graph_plans_without_external_effects() -> None:
    nodes = [_node("epic-232", title="Control-plane truth hardening")]
    edges: list[IssueEdgeDraft] = []
    previous = None
    for number in range(233, 245):
        ref = f"task-{number}"
        nodes.append(_node(ref, parent_ref="epic-232", status="planned"))
        if previous is not None:
            edges.append(IssueEdgeDraft(ref, previous, IssueEdgeKind.BLOCKED_BY))
        previous = ref
    proposal = plan_issue_graph(
        IssueGraphDraft("repoforge", "epic-232", tuple(nodes), tuple(edges)),
        _identity(),
        live_issues=(),
        created_at="2026-07-22T00:00:00+00:00",
        expires_at="2026-07-23T00:00:00+00:00",
    )

    assert len(proposal.rendered_nodes) == 13
    assert len(proposal.revision_comments) == 13
    assert proposal.external_writes == 0
    assert proposal.publication_order[0] == "node:epic-232"
    assert proposal.publication_order[-1] == "checklist:epic-232"
    assert all("<!-- repoforge-issue:" in body for _, body in proposal.rendered_nodes)
    rendered = dict(proposal.rendered_nodes)
    assert "## Delivery checklist" in rendered["epic-232"]
    assert "- [ ] task-233" in rendered["epic-232"]
    assert "Parent: `epic-232`" in rendered["task-233"]
    assert "Blocked by: `task-233`" in rendered["task-234"]
    assert all("<!-- repoforge-revision:" in body for _, body in proposal.revision_comments)
