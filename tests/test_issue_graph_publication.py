from __future__ import annotations

from dataclasses import dataclass, replace

import pytest
from conftest import ForgeEnvironment

from repoforge.adapters.locking import FcntlLockManager
from repoforge.adapters.persistence.json_issue_graph_proposal_store import (
    JsonIssueGraphProposalStore,
)
from repoforge.adapters.persistence.json_issue_graph_publication_store import (
    JsonIssueGraphPublicationStore,
)
from repoforge.application.repository.issue_graph_publication import (
    IssueGraphPublicationCoordinator,
    PublicationEffectFailure,
    PublicationRateLimited,
    StepEffectResult,
)
from repoforge.domain.errors import ErrorCode, RepoForgeError
from repoforge.domain.execution_receipt import EffectReceiptState
from repoforge.domain.issue_graph_proposal import (
    IssueEdgeDraft,
    IssueEdgeKind,
    IssueGraphDraft,
    IssueGraphIdentity,
    IssueNodeDraft,
    LiveIssueCandidate,
    plan_issue_graph,
)
from repoforge.domain.issue_graph_publication import (
    IssueGraphPublicationStep,
    PublicationLiveGraph,
    PublicationLiveNode,
    PublicationProviderIdentity,
    PublicationState,
    PublicationStepKind,
    PublicationStepState,
    build_issue_graph_publication_plan,
)
from repoforge.domain.operation_task import OperationState


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


def _proposal():
    root = IssueNodeDraft(
        "epic-232",
        "Control-plane truth hardening",
        "epic",
        "p0",
        "ready",
        None,
        "## Objective\n\nDeliver the epic.\n\n## Acceptance criteria\n\n- [ ] Done.\n",
    )
    child = IssueNodeDraft(
        "task-233",
        "Task 233",
        "task",
        "p0",
        "ready",
        "epic-232",
        "## Objective\n\nDeliver task 233.\n\n## Acceptance criteria\n\n- [ ] Done.\n",
    )
    tail = IssueNodeDraft(
        "task-234",
        "Task 234",
        "task",
        "p0",
        "ready",
        "epic-232",
        "## Objective\n\nDeliver task 234.\n\n## Acceptance criteria\n\n- [ ] Done.\n",
    )
    return plan_issue_graph(
        IssueGraphDraft(
            "repoforge",
            "epic-232",
            (root, child, tail),
            (IssueEdgeDraft("task-234", "task-233", IssueEdgeKind.BLOCKED_BY),),
        ),
        _identity(),
        live_issues=(
            LiveIssueCandidate(
                232,
                "Old epic title",
                "<!-- repoforge-issue:epic-232 -->",
            ),
            LiveIssueCandidate(999, "Task 234", None),
        ),
        created_at="2026-07-22T00:00:00+00:00",
        expires_at="2026-07-23T00:00:00+00:00",
    )


def _live() -> PublicationLiveGraph:
    return PublicationLiveGraph(
        nodes=(
            PublicationLiveNode(
                "epic-232",
                232,
                100232,
                True,
                "Old epic title",
                "<!-- repoforge-issue:epic-232 -->\n\nold",
            ),
            PublicationLiveNode("task-234", 999, 100999, False, "Task 234", "unmanaged"),
        ),
        parent_by_ref=(("task-234", "legacy-parent"),),
        blocked_by_refs=(("task-234", "legacy-blocker"),),
        snapshot_sha256="3" * 64,
    )


@dataclass
class ScriptedExecutor:
    calls: list[str]
    reconciles: list[str]
    applied: dict[str, StepEffectResult]
    failures: dict[str, list[Exception]]
    next_issue: int = 300
    capability_hash: str = "7" * 64

    def provider_identity(self) -> PublicationProviderIdentity:
        return PublicationProviderIdentity(
            provider="github",
            api_version="2022-11-28",
            media_type="application/vnd.github+json",
            adapter="scripted-test",
            capability_hash=self.capability_hash,
        )

    def apply(
        self,
        step: IssueGraphPublicationStep,
        mapping: dict[str, int],
    ) -> StepEffectResult:
        self.calls.append(step.step_id)
        queued = self.failures.get(step.step_id, [])
        if queued:
            failure = queued.pop(0)
            if isinstance(failure, PublicationEffectFailure) and failure.effect_boundary_crossed:
                result = self._result(step, mapping, state=PublicationStepState.APPLIED)
                self.applied[step.step_id] = result
            raise failure
        result = self._result(step, mapping, state=PublicationStepState.APPLIED)
        self.applied[step.step_id] = result
        return result

    def reconcile(
        self,
        step: IssueGraphPublicationStep,
        mapping: dict[str, int],
    ) -> StepEffectResult | None:
        self.reconciles.append(step.step_id)
        existing = self.applied.get(step.step_id)
        if existing is None:
            return None
        return replace(existing, state=PublicationStepState.RECONCILED_EXISTING, external_writes=0)

    def _result(
        self,
        step: IssueGraphPublicationStep,
        mapping: dict[str, int],
        *,
        state: PublicationStepState,
    ) -> StepEffectResult:
        issue_number = mapping.get(step.source_ref)
        if step.kind is PublicationStepKind.CREATE_NODE:
            issue_number = self.next_issue
            self.next_issue += 1
        return StepEffectResult(
            state=state,
            issue_number=issue_number,
            operation_id=f"op-{'a' * 23}{step.ordinal % 10}",
            receipt_id=f"receipt-{'b' * 23}{step.ordinal % 10}",
            result_reference=f"issue-graph-step:{step.step_id}",
            external_writes=1 if state is PublicationStepState.APPLIED else 0,
            provider_identity=self.provider_identity(),
        )


def _coordinator(
    forge_env: ForgeEnvironment,
    tmp_path,
    executor: ScriptedExecutor,
) -> tuple[IssueGraphPublicationCoordinator, object]:
    locks = FcntlLockManager(tmp_path / "publication-locks")
    proposals = JsonIssueGraphProposalStore(tmp_path / "publication-state", locks)
    publications = JsonIssueGraphPublicationStore(tmp_path / "publication-state", locks)
    proposal = _proposal()
    proposals.create(proposal)
    return (
        IssueGraphPublicationCoordinator(
            forge_env.service.application.context,
            forge_env.service.operations,
            proposals,
            publications,
            executor,
        ),
        proposal,
    )


def _prepare(
    coordinator: IssueGraphPublicationCoordinator,
    proposal,
    *,
    adopt_refs: tuple[str, ...] = ("task-234",),
):
    return coordinator.prepare(
        proposal.proposal_id,
        _identity(),
        live_graph=_live(),
        adopt_refs=adopt_refs,
        created_at="2026-07-22T00:00:00+00:00",
        expires_at="2026-07-23T00:00:00+00:00",
    )


def test_publication_plan_is_deterministic_and_requires_explicit_adoption(
    forge_env: ForgeEnvironment,
    tmp_path,
) -> None:
    executor = ScriptedExecutor([], [], {}, {})
    coordinator, proposal = _coordinator(forge_env, tmp_path, executor)

    with pytest.raises(RepoForgeError) as blocked:
        _prepare(coordinator, proposal, adopt_refs=())
    assert blocked.value.code is ErrorCode.PROPOSAL_BLOCKED
    assert blocked.value.details["findings"][0]["code"] == "ADOPTION_REQUIRED"

    first = _prepare(coordinator, proposal)
    second = _prepare(coordinator, proposal)
    assert first.effect_plan_hash == second.effect_plan_hash
    assert first.steps == second.steps
    assert first.initial_mapping == (("epic-232", 232), ("task-234", 999))
    assert tuple(step.kind for step in first.steps[:3]) == (
        PublicationStepKind.UPDATE_NODE,
        PublicationStepKind.CREATE_NODE,
        PublicationStepKind.ADOPT_NODE,
    )
    assert first.steps[-1].kind is PublicationStepKind.UPDATE_EPIC
    assert executor.calls == []


def test_accept_persists_operation_and_receipt_before_any_effect(
    forge_env: ForgeEnvironment,
    tmp_path,
) -> None:
    executor = ScriptedExecutor([], [], {}, {})
    coordinator, proposal = _coordinator(forge_env, tmp_path, executor)
    plan = _prepare(coordinator, proposal)

    publication = coordinator.accept(
        plan.plan_id,
        approved_proposal_hash=proposal.proposal_hash,
        approved_effect_plan_hash=plan.effect_plan_hash,
        actual_identity=_identity(),
        now="2026-07-22T00:00:00+00:00",
    )

    assert executor.calls == []
    assert publication.state is PublicationState.RUNNING
    assert all(step.state is PublicationStepState.PENDING for step in publication.steps)
    operation = forge_env.service.operations.status(publication.operation_id)
    receipt = forge_env.service.application.context.effect_receipts.read(publication.receipt_id)
    assert operation.state.value == "running"
    assert operation.receipt_id == publication.receipt_id
    assert receipt is not None
    assert receipt.value.state.value == "applying"


def test_resume_is_idempotent_and_returns_exact_mapping(
    forge_env: ForgeEnvironment,
    tmp_path,
) -> None:
    executor = ScriptedExecutor([], [], {}, {})
    coordinator, proposal = _coordinator(forge_env, tmp_path, executor)
    plan = _prepare(coordinator, proposal)
    accepted = coordinator.accept(
        plan.plan_id,
        approved_proposal_hash=proposal.proposal_hash,
        approved_effect_plan_hash=plan.effect_plan_hash,
        actual_identity=_identity(),
        now="2026-07-22T00:00:00+00:00",
    )

    completed = coordinator.resume(accepted.publication_id, _identity())
    first_call_count = len(executor.calls)
    replay = coordinator.resume(accepted.publication_id, _identity())

    assert completed.state is PublicationState.SUCCEEDED
    assert replay == completed
    assert len(executor.calls) == first_call_count
    assert dict(completed.node_mapping) == {
        "epic-232": 232,
        "task-233": 300,
        "task-234": 999,
    }
    assert completed.result_reference == f"issue-graph-publication:{completed.publication_id}"
    assert all(
        step.state in {PublicationStepState.APPLIED, PublicationStepState.RECONCILED_EXISTING}
        for step in completed.steps
    )


def test_failed_before_effect_retries_the_same_step(
    forge_env: ForgeEnvironment,
    tmp_path,
) -> None:
    executor = ScriptedExecutor([], [], {}, {})
    coordinator, proposal = _coordinator(forge_env, tmp_path, executor)
    plan = _prepare(coordinator, proposal)
    executor.failures[plan.steps[0].step_id] = [
        PublicationEffectFailure("network unavailable", effect_boundary_crossed=False)
    ]
    accepted = coordinator.accept(
        plan.plan_id,
        approved_proposal_hash=proposal.proposal_hash,
        approved_effect_plan_hash=plan.effect_plan_hash,
        actual_identity=_identity(),
        now="2026-07-22T00:00:00+00:00",
    )

    failed = coordinator.resume(accepted.publication_id, _identity())
    assert failed.steps[0].state is PublicationStepState.FAILED_BEFORE_EFFECT
    resumed = coordinator.resume(accepted.publication_id, _identity())
    assert resumed.state is PublicationState.SUCCEEDED
    assert executor.calls.count(plan.steps[0].step_id) == 2


def test_failed_after_effect_reconciles_without_duplicate_write(
    forge_env: ForgeEnvironment,
    tmp_path,
) -> None:
    executor = ScriptedExecutor([], [], {}, {})
    coordinator, proposal = _coordinator(forge_env, tmp_path, executor)
    plan = _prepare(coordinator, proposal)
    executor.failures[plan.steps[0].step_id] = [
        PublicationEffectFailure("response lost", effect_boundary_crossed=True)
    ]
    accepted = coordinator.accept(
        plan.plan_id,
        approved_proposal_hash=proposal.proposal_hash,
        approved_effect_plan_hash=plan.effect_plan_hash,
        actual_identity=_identity(),
        now="2026-07-22T00:00:00+00:00",
    )

    uncertain = coordinator.resume(accepted.publication_id, _identity())
    assert uncertain.steps[0].state is PublicationStepState.FAILED_AFTER_EFFECT
    resumed = coordinator.resume(accepted.publication_id, _identity())

    assert resumed.state is PublicationState.SUCCEEDED
    assert executor.calls.count(plan.steps[0].step_id) == 1
    assert executor.reconciles.count(plan.steps[0].step_id) == 1
    assert resumed.steps[0].state is PublicationStepState.RECONCILED_EXISTING


def test_rate_limit_pauses_and_resumes_the_same_operation(
    forge_env: ForgeEnvironment,
    tmp_path,
) -> None:
    executor = ScriptedExecutor([], [], {}, {})
    coordinator, proposal = _coordinator(forge_env, tmp_path, executor)
    plan = _prepare(coordinator, proposal)
    retry_at = "2026-07-22T00:05:00+00:00"
    executor.failures[plan.steps[0].step_id] = [PublicationRateLimited(retry_at)]
    accepted = coordinator.accept(
        plan.plan_id,
        approved_proposal_hash=proposal.proposal_hash,
        approved_effect_plan_hash=plan.effect_plan_hash,
        actual_identity=_identity(),
        now="2026-07-22T00:00:00+00:00",
    )

    paused = coordinator.resume(
        accepted.publication_id,
        _identity(),
        now="2026-07-22T00:00:00+00:00",
    )
    assert paused.state is PublicationState.PAUSED
    assert paused.retry_at == retry_at
    assert paused.steps[0].state is PublicationStepState.PAUSED_RATE_LIMIT
    same_operation = paused.operation_id

    still_paused = coordinator.resume(
        accepted.publication_id,
        _identity(),
        now="2026-07-22T00:04:59+00:00",
    )
    assert still_paused == paused
    resumed = coordinator.resume(
        accepted.publication_id,
        _identity(),
        now="2026-07-22T00:05:00+00:00",
    )
    assert resumed.state is PublicationState.SUCCEEDED
    assert resumed.operation_id == same_operation


def test_unconfirmed_effect_enters_terminal_manual_recovery_without_blind_retry(
    forge_env: ForgeEnvironment,
    tmp_path,
) -> None:
    executor = ScriptedExecutor([], [], {}, {})
    coordinator, proposal = _coordinator(forge_env, tmp_path, executor)
    plan = _prepare(coordinator, proposal)
    executor.failures[plan.steps[0].step_id] = [
        PublicationEffectFailure("response lost", effect_boundary_crossed=True)
    ]
    accepted = coordinator.accept(
        plan.plan_id,
        approved_proposal_hash=proposal.proposal_hash,
        approved_effect_plan_hash=plan.effect_plan_hash,
        actual_identity=_identity(),
        now="2026-07-22T00:00:00+00:00",
    )

    uncertain = coordinator.resume(accepted.publication_id, _identity())
    assert uncertain.steps[0].state is PublicationStepState.FAILED_AFTER_EFFECT
    executor.applied.clear()
    manual = coordinator.resume(accepted.publication_id, _identity())
    call_count = len(executor.calls)
    reconcile_count = len(executor.reconciles)
    replay = coordinator.resume(accepted.publication_id, _identity())

    assert manual.state is PublicationState.MANUAL_RECOVERY_REQUIRED
    assert manual.steps[0].state is PublicationStepState.MANUAL_RECOVERY_REQUIRED
    assert replay == manual
    assert len(executor.calls) == call_count
    assert len(executor.reconciles) == reconcile_count
    operation = forge_env.service.operations.status(manual.operation_id)
    assert operation.state is OperationState.FAILED
    receipt_store = forge_env.service.application.context.effect_receipts
    assert receipt_store is not None
    receipt = receipt_store.read(manual.receipt_id)
    assert receipt is not None
    assert receipt.value.state is EffectReceiptState.UNKNOWN


def test_stale_identity_and_changed_approval_hashes_fail_before_mutation(
    forge_env: ForgeEnvironment,
    tmp_path,
) -> None:
    executor = ScriptedExecutor([], [], {}, {})
    coordinator, proposal = _coordinator(forge_env, tmp_path, executor)
    plan = _prepare(coordinator, proposal)

    with pytest.raises(RepoForgeError) as approval:
        coordinator.accept(
            plan.plan_id,
            approved_proposal_hash="8" * 64,
            approved_effect_plan_hash=plan.effect_plan_hash,
            actual_identity=_identity(),
            now="2026-07-22T00:00:00+00:00",
        )
    assert approval.value.code is ErrorCode.APPROVAL_MISMATCH

    with pytest.raises(RepoForgeError) as stale:
        coordinator.accept(
            plan.plan_id,
            approved_proposal_hash=proposal.proposal_hash,
            approved_effect_plan_hash=plan.effect_plan_hash,
            actual_identity=_identity(active_generation=13),
            now="2026-07-22T00:00:00+00:00",
        )
    assert stale.value.code is ErrorCode.CONFIG_STALE

    with pytest.raises(RepoForgeError) as expired:
        coordinator.accept(
            plan.plan_id,
            approved_proposal_hash=proposal.proposal_hash,
            approved_effect_plan_hash=plan.effect_plan_hash,
            actual_identity=_identity(),
            now=plan.expires_at,
        )
    assert expired.value.code is ErrorCode.CONFIG_STALE

    executor.capability_hash = "8" * 64
    with pytest.raises(RepoForgeError) as provider_stale:
        coordinator.accept(
            plan.plan_id,
            approved_proposal_hash=proposal.proposal_hash,
            approved_effect_plan_hash=plan.effect_plan_hash,
            actual_identity=_identity(),
            now="2026-07-22T00:00:00+00:00",
        )
    assert provider_stale.value.code is ErrorCode.CONFIG_STALE
    assert executor.calls == []


def test_every_unmanaged_mapped_node_requires_explicit_adoption(
    forge_env: ForgeEnvironment,
    tmp_path,
) -> None:
    executor = ScriptedExecutor([], [], {}, {})
    coordinator, proposal = _coordinator(forge_env, tmp_path, executor)
    live = _live()
    expanded = replace(
        live,
        nodes=(
            *live.nodes,
            PublicationLiveNode(
                "task-233",
                233,
                100233,
                False,
                "Task 233",
                "unmanaged task 233",
            ),
        ),
    )

    with pytest.raises(RepoForgeError) as blocked:
        coordinator.prepare(
            proposal.proposal_id,
            _identity(),
            live_graph=expanded,
            adopt_refs=("task-234",),
            created_at="2026-07-22T00:00:00+00:00",
            expires_at="2026-07-23T00:00:00+00:00",
        )

    findings = blocked.value.details["findings"]
    assert any(
        item["code"] == "ADOPTION_REQUIRED" and item["path"] == "node:task-233" for item in findings
    )


def test_dependency_plan_preserves_multiple_desired_blockers_and_removes_only_stale_edges() -> None:
    root = IssueNodeDraft(
        "root",
        "Root",
        "epic",
        "p0",
        "ready",
        None,
        "## Objective\n\nRoot.\n\n## Acceptance criteria\n\n- [ ] Done.\n",
    )
    source = IssueNodeDraft(
        "source",
        "Source",
        "task",
        "p0",
        "ready",
        "root",
        "## Objective\n\nSource.\n\n## Acceptance criteria\n\n- [ ] Done.\n",
    )
    blocker_a = IssueNodeDraft(
        "blocker-a",
        "Blocker A",
        "task",
        "p0",
        "ready",
        "root",
        "## Objective\n\nA.\n\n## Acceptance criteria\n\n- [ ] Done.\n",
    )
    blocker_b = IssueNodeDraft(
        "blocker-b",
        "Blocker B",
        "task",
        "p0",
        "ready",
        "root",
        "## Objective\n\nB.\n\n## Acceptance criteria\n\n- [ ] Done.\n",
    )
    proposal = plan_issue_graph(
        IssueGraphDraft(
            "repoforge",
            "root",
            (root, source, blocker_a, blocker_b),
            (
                IssueEdgeDraft("source", "blocker-a", IssueEdgeKind.BLOCKED_BY),
                IssueEdgeDraft("source", "blocker-b", IssueEdgeKind.BLOCKED_BY),
            ),
        ),
        _identity(),
        live_issues=(),
        created_at="2026-07-22T00:00:00+00:00",
        expires_at="2026-07-23T00:00:00+00:00",
    )
    live = PublicationLiveGraph(
        nodes=(),
        blocked_by_refs=(
            ("source", "blocker-a"),
            ("source", "legacy-blocker"),
        ),
        snapshot_sha256="3" * 64,
    )

    plan = build_issue_graph_publication_plan(
        proposal,
        _identity(),
        live_graph=live,
        adopt_refs=(),
        provider_identity=PublicationProviderIdentity(
            provider="github",
            api_version="2022-11-28",
            media_type="application/vnd.github+json",
            adapter="test",
            capability_hash="7" * 64,
        ),
        created_at="2026-07-22T00:00:00+00:00",
        expires_at="2026-07-23T00:00:00+00:00",
    )

    dependency_steps = tuple(
        (step.kind, step.source_ref, step.target_ref)
        for step in plan.steps
        if step.kind
        in {
            PublicationStepKind.ADD_DEPENDENCY,
            PublicationStepKind.REMOVE_DEPENDENCY,
        }
    )
    assert dependency_steps == (
        (PublicationStepKind.REMOVE_DEPENDENCY, "source", "legacy-blocker"),
        (PublicationStepKind.ADD_DEPENDENCY, "source", "blocker-b"),
    )


def test_terminal_publication_records_are_immutable(
    forge_env: ForgeEnvironment,
    tmp_path,
) -> None:
    executor = ScriptedExecutor([], [], {}, {})
    coordinator, proposal = _coordinator(forge_env, tmp_path, executor)
    plan = _prepare(coordinator, proposal)
    accepted = coordinator.accept(
        plan.plan_id,
        approved_proposal_hash=proposal.proposal_hash,
        approved_effect_plan_hash=plan.effect_plan_hash,
        actual_identity=_identity(),
        now="2026-07-22T00:00:00+00:00",
    )
    completed = coordinator.resume(accepted.publication_id, _identity())
    envelope = coordinator.publications.read_publication(completed.publication_id)
    assert envelope is not None

    with pytest.raises(RepoForgeError) as rewrite:
        coordinator.publications.save_publication(
            replace(completed, state=PublicationState.RUNNING),
            expected_revision=envelope.revision,
        )

    assert rewrite.value.code is ErrorCode.STATE_INVALID
