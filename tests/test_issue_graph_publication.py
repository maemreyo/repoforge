from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from threading import Barrier

import pytest
from conftest import ForgeEnvironment

from repoforge.adapters.locking import FcntlLockManager
from repoforge.adapters.persistence.json_issue_graph_proposal_store import (
    JsonIssueGraphProposalStore,
)
from repoforge.adapters.persistence.json_issue_graph_publication_store import (
    JsonIssueGraphPublicationStore,
)
from repoforge.application.idempotency import IdempotencyEffectBoundary
from repoforge.application.repository.issue_graph_publication import (
    IssueGraphPublicationCoordinator,
    PublicationEffectFailure,
    PublicationRateLimited,
    RepositoryIssueGraphStepExecutor,
    StepEffectResult,
)
from repoforge.domain.errors import ErrorCode, RepoForgeError
from repoforge.domain.execution_receipt import EffectReceiptState, transition_effect_receipt
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
    IssueGraphPublication,
    IssueGraphPublicationStep,
    PublicationLiveGraph,
    PublicationLiveNode,
    PublicationProviderIdentity,
    PublicationState,
    PublicationStepKind,
    PublicationStepState,
    build_issue_graph_publication_plan,
    materialize_issue_graph_body,
    publication_from_payload,
    publication_payload,
    publication_plan_from_payload,
    publication_plan_payload,
)
from repoforge.domain.operation_task import OperationRetryability, OperationState
from repoforge.ports.issue_mutation import RemoteIssue


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
        existing = self.applied.get(step.step_id)
        if existing is not None:
            return replace(existing, writes_this_call=0)
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
        return replace(
            existing,
            state=PublicationStepState.RECONCILED_EXISTING,
            writes_this_call=0,
        )

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
            writes_this_call=1 if state is PublicationStepState.APPLIED else 0,
            provider_identity=self.provider_identity(),
        )


def _coordinator(
    forge_env: ForgeEnvironment,
    tmp_path,
    executor: ScriptedExecutor,
    *,
    max_writes_per_call: int = 20,
) -> tuple[IssueGraphPublicationCoordinator, object]:
    locks = FcntlLockManager(tmp_path / "publication-locks")
    proposals = JsonIssueGraphProposalStore(tmp_path / "publication-state", locks)
    publications = JsonIssueGraphPublicationStore(tmp_path / "publication-state", locks)
    proposal = _proposal()
    proposals.create(proposal)
    original = forge_env.service.application.context
    repository = original.config.repositories["demo"]
    policy = replace(
        repository.issue_writes,
        enabled_ops=("link", "create", "update"),
        operation_semantics_version=2,
        max_writes_per_call=max_writes_per_call,
        max_writes_per_window=max(
            repository.issue_writes.max_writes_per_window, max_writes_per_call
        ),
    )
    config = replace(
        original.config,
        repositories={
            **original.config.repositories,
            "demo": replace(repository, issue_writes=policy),
            "repoforge": replace(repository, issue_writes=policy),
        },
    )
    return (
        IssueGraphPublicationCoordinator(
            replace(original, config=config),
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


class _BarrierLockManager:
    """Release concurrent contenders together immediately before the keyed lock."""

    def __init__(self, root: Path) -> None:
        self._delegate = FcntlLockManager(root)
        self._barrier = Barrier(2)

    def path_for(self, name: str) -> Path:
        return self._delegate.path_for(name)

    @contextmanager
    def lock(
        self,
        name: str,
        *,
        timeout_seconds: float | None = None,
        metadata: dict[str, str] | None = None,
    ) -> Iterator[None]:
        self._barrier.wait(timeout=5)
        with self._delegate.lock(
            name,
            timeout_seconds=timeout_seconds,
            metadata=metadata,
        ):
            yield


def _publication_plan():
    return build_issue_graph_publication_plan(
        _proposal(),
        _identity(),
        live_graph=_live(),
        adopt_refs=("task-234",),
        provider_identity=PublicationProviderIdentity(
            provider="github",
            api_version="2022-11-28",
            media_type="application/vnd.github+json",
            adapter="race-test",
            capability_hash="7" * 64,
        ),
        created_at="2026-07-22T00:00:00+00:00",
        expires_at="2026-07-23T00:00:00+00:00",
    )


def _publication(plan) -> IssueGraphPublication:
    return IssueGraphPublication(
        publication_id=f"igpub-{'a' * 24}",
        plan_id=plan.plan_id,
        proposal_id=plan.proposal_id,
        proposal_hash=plan.proposal_hash,
        effect_plan_hash=plan.effect_plan_hash,
        identity=plan.identity,
        provider_identity=plan.provider_identity,
        state=PublicationState.RUNNING,
        steps=plan.steps,
        node_mapping=plan.initial_mapping,
        operation_id=f"op-{'b' * 24}",
        receipt_id=f"receipt-{'c' * 24}",
        result_reference=None,
        retry_at=None,
        external_writes=0,
        created_at="2026-07-22T00:00:00+00:00",
        updated_at="2026-07-22T00:00:00+00:00",
        expires_at="2026-07-23T00:00:00+00:00",
    )


def _results_or_errors(
    futures: tuple[Future[object], Future[object]],
) -> tuple[list[object], list[RepoForgeError]]:
    results: list[object] = []
    errors: list[RepoForgeError] = []
    for future in futures:
        try:
            results.append(future.result(timeout=10))
        except RepoForgeError as exc:
            errors.append(exc)
    return results, errors


def test_concurrent_identical_plan_creates_return_same_durable_envelope(tmp_path: Path) -> None:
    state_root = tmp_path / "publication-state"
    lock_root = tmp_path / "publication-locks"
    store = JsonIssueGraphPublicationStore(state_root, _BarrierLockManager(lock_root))
    plan = _publication_plan()

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = (
            executor.submit(store.create_plan, plan),
            executor.submit(store.create_plan, plan),
        )
        results, errors = _results_or_errors(futures)

    assert errors == []
    assert len(results) == 2
    assert results[0] == results[1]
    restarted = JsonIssueGraphPublicationStore(state_root, FcntlLockManager(lock_root))
    assert restarted.read_plan(plan.plan_id) == results[0]


def test_concurrent_conflicting_plan_creates_persist_one_deterministic_winner(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "publication-state"
    lock_root = tmp_path / "publication-locks"
    store = JsonIssueGraphPublicationStore(state_root, _BarrierLockManager(lock_root))
    plan = _publication_plan()
    conflicting = replace(plan, expires_at="2026-07-24T00:00:00+00:00")

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = (
            executor.submit(store.create_plan, plan),
            executor.submit(store.create_plan, conflicting),
        )
        results, errors = _results_or_errors(futures)

    assert len(results) == 1
    assert len(errors) == 1
    assert errors[0].code is ErrorCode.ALREADY_EXISTS
    restarted = JsonIssueGraphPublicationStore(state_root, FcntlLockManager(lock_root))
    assert restarted.read_plan(plan.plan_id) == results[0]


def test_concurrent_identical_publication_creates_return_same_durable_envelope(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "publication-state"
    lock_root = tmp_path / "publication-locks"
    store = JsonIssueGraphPublicationStore(state_root, _BarrierLockManager(lock_root))
    publication = _publication(_publication_plan())

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = (
            executor.submit(store.create_publication, publication),
            executor.submit(store.create_publication, publication),
        )
        results, errors = _results_or_errors(futures)

    assert errors == []
    assert len(results) == 2
    assert results[0] == results[1]
    restarted = JsonIssueGraphPublicationStore(state_root, FcntlLockManager(lock_root))
    assert restarted.read_publication(publication.publication_id) == results[0]


def test_concurrent_conflicting_publication_creates_persist_one_deterministic_winner(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "publication-state"
    lock_root = tmp_path / "publication-locks"
    store = JsonIssueGraphPublicationStore(state_root, _BarrierLockManager(lock_root))
    publication = _publication(_publication_plan())
    conflicting = replace(publication, updated_at="2026-07-22T00:00:01+00:00")

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = (
            executor.submit(store.create_publication, publication),
            executor.submit(store.create_publication, conflicting),
        )
        results, errors = _results_or_errors(futures)

    assert len(results) == 1
    assert len(errors) == 1
    assert errors[0].code is ErrorCode.ALREADY_EXISTS
    restarted = JsonIssueGraphPublicationStore(state_root, FcntlLockManager(lock_root))
    assert restarted.read_publication(publication.publication_id) == results[0]


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
        PublicationStepKind.CREATE_NODE,
        PublicationStepKind.UPDATE_NODE,
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


def test_resume_honors_per_call_write_budget_and_reconciles_without_spending_it(
    forge_env: ForgeEnvironment,
    tmp_path,
) -> None:
    executor = ScriptedExecutor([], [], {}, {})
    coordinator, proposal = _coordinator(
        forge_env,
        tmp_path,
        executor,
        max_writes_per_call=1,
    )
    plan = _prepare(coordinator, proposal)
    reconciled_step = plan.steps[1]
    executor.applied[reconciled_step.step_id] = executor._result(
        reconciled_step,
        dict(plan.initial_mapping),
        state=PublicationStepState.APPLIED,
    )
    accepted = coordinator.accept(
        plan.plan_id,
        approved_proposal_hash=proposal.proposal_hash,
        approved_effect_plan_hash=plan.effect_plan_hash,
        actual_identity=_identity(),
        now="2026-07-22T00:00:00+00:00",
    )

    snapshots: list[IssueGraphPublication] = []
    for _ in range(len(plan.steps) + 1):
        snapshot = coordinator.resume(accepted.publication_id, _identity())
        snapshots.append(snapshot)
        assert snapshot.operation_id == accepted.operation_id
        assert snapshot.receipt_id == accepted.receipt_id
        if snapshot.state is PublicationState.SUCCEEDED:
            break

    first = snapshots[0]
    assert first.state is PublicationState.RUNNING
    assert first.retry_at is None
    assert first.steps[0].state is PublicationStepState.APPLIED
    assert first.steps[1].state is PublicationStepState.RECONCILED_EXISTING
    assert first.steps[2].state is PublicationStepState.PENDING
    assert snapshots[-1].state is PublicationState.SUCCEEDED
    assert len(executor.applied) == snapshots[-1].external_writes
    assert len(executor.calls) == len(set(executor.calls))


def test_cached_step_replay_does_not_spend_the_new_call_budget(
    forge_env: ForgeEnvironment,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executor = ScriptedExecutor([], [], {}, {})
    coordinator, proposal = _coordinator(
        forge_env,
        tmp_path,
        executor,
        max_writes_per_call=1,
    )
    plan = _prepare(coordinator, proposal)
    accepted = coordinator.accept(
        plan.plan_id,
        approved_proposal_hash=proposal.proposal_hash,
        approved_effect_plan_hash=plan.effect_plan_hash,
        actual_identity=_identity(),
        now="2026-07-22T00:00:00+00:00",
    )
    original_save = coordinator._save_step
    injected = False

    def crash_before_step_save(*args, **kwargs):
        nonlocal injected
        if not injected:
            injected = True
            raise RuntimeError("fault after effect before step save")
        return original_save(*args, **kwargs)

    monkeypatch.setattr(coordinator, "_save_step", crash_before_step_save)
    with pytest.raises(RuntimeError, match="fault after effect"):
        coordinator.resume(accepted.publication_id, _identity())
    monkeypatch.setattr(coordinator, "_save_step", original_save)

    resumed = coordinator.resume(accepted.publication_id, _identity())

    assert resumed.state is PublicationState.RUNNING
    assert resumed.external_writes == 2
    assert resumed.steps[0].state is PublicationStepState.APPLIED
    assert resumed.steps[1].state is PublicationStepState.APPLIED
    assert resumed.steps[2].state is PublicationStepState.PENDING
    assert len(executor.applied) == 2


@pytest.mark.parametrize(
    "fault_after",
    ["receipt_unvalidated", "receipt_validated", "operation_succeeded"],
)
def test_success_finalization_resumes_from_each_durable_checkpoint(
    forge_env: ForgeEnvironment,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    fault_after: str,
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
    receipt_store = forge_env.service.application.context.effect_receipts
    assert receipt_store is not None

    if fault_after.startswith("receipt_"):
        target = (
            EffectReceiptState.APPLIED_UNVALIDATED
            if fault_after == "receipt_unvalidated"
            else EffectReceiptState.APPLIED_VALIDATED
        )
        original_save = receipt_store.save
        injected = False

        def save_receipt_then_crash(receipt, *, expected_revision=None):
            nonlocal injected
            saved = original_save(receipt, expected_revision=expected_revision)
            if not injected and saved.value.state is target:
                injected = True
                raise RuntimeError(f"fault after {target.value}")
            return saved

        monkeypatch.setattr(receipt_store, "save", save_receipt_then_crash)
        with pytest.raises(RuntimeError, match="fault after"):
            coordinator.resume(accepted.publication_id, _identity())
        monkeypatch.setattr(receipt_store, "save", original_save)
    else:
        original_save_publication = coordinator.publications.save_publication
        injected = False

        def save_publication_after_operation(*args, **kwargs):
            nonlocal injected
            publication = args[0]
            if not injected and publication.state is PublicationState.SUCCEEDED:
                injected = True
                raise RuntimeError("fault after operation succeeded")
            return original_save_publication(*args, **kwargs)

        monkeypatch.setattr(
            coordinator.publications,
            "save_publication",
            save_publication_after_operation,
        )
        with pytest.raises(RuntimeError, match="fault after operation"):
            coordinator.resume(accepted.publication_id, _identity())
        monkeypatch.setattr(
            coordinator.publications,
            "save_publication",
            original_save_publication,
        )

    completed = coordinator.resume(accepted.publication_id, _identity())
    replay = coordinator.resume(accepted.publication_id, _identity())

    assert completed.state is PublicationState.SUCCEEDED
    assert replay == completed
    receipt = receipt_store.read(accepted.receipt_id)
    assert receipt is not None
    assert receipt.value.state is EffectReceiptState.APPLIED_VALIDATED
    assert receipt.value.result_reference == completed.result_reference
    operation = coordinator.operations.status(accepted.operation_id)
    assert operation.state is OperationState.SUCCEEDED
    assert operation.result_reference == completed.result_reference
    assert operation.receipt_id == accepted.receipt_id


def test_success_finalization_rejects_conflicting_terminal_receipt_without_rewrite(
    forge_env: ForgeEnvironment,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
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
    receipt_store = forge_env.service.application.context.effect_receipts
    assert receipt_store is not None
    original_save = receipt_store.save
    injected = False

    def save_unvalidated_then_crash(receipt, *, expected_revision=None):
        nonlocal injected
        saved = original_save(receipt, expected_revision=expected_revision)
        if not injected and saved.value.state is EffectReceiptState.APPLIED_UNVALIDATED:
            injected = True
            raise RuntimeError("fault after applied_unvalidated")
        return saved

    monkeypatch.setattr(receipt_store, "save", save_unvalidated_then_crash)
    with pytest.raises(RuntimeError, match="fault after applied_unvalidated"):
        coordinator.resume(accepted.publication_id, _identity())
    monkeypatch.setattr(receipt_store, "save", original_save)
    receipt = receipt_store.read(accepted.receipt_id)
    assert receipt is not None
    conflicting = original_save(
        transition_effect_receipt(
            receipt.value,
            EffectReceiptState.UNKNOWN,
            now="2026-07-22T00:00:05+00:00",
            result_reference=receipt.value.result_reference,
            error_code=ErrorCode.EFFECT_OUTCOME_UNKNOWN.value,
            error_message="conflicting terminal evidence",
            effect_boundary_crossed=True,
        ),
        expected_revision=receipt.revision,
    )

    with pytest.raises(RepoForgeError) as unsafe:
        coordinator.resume(accepted.publication_id, _identity())

    assert unsafe.value.code is ErrorCode.WORKFLOW_REPLAY_UNSAFE
    assert receipt_store.read(accepted.receipt_id) == conflicting
    operation = coordinator.operations.status(accepted.operation_id)
    assert operation.state is OperationState.FAILED
    assert operation.retryability is OperationRetryability.MANUAL
    publication = coordinator.publications.read_publication(accepted.publication_id)
    assert publication is not None
    assert publication.value.state is PublicationState.MANUAL_RECOVERY_REQUIRED


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


def test_materialize_issue_graph_body_rewrites_only_managed_graph_refs() -> None:
    body = """<!-- repoforge-issue:source -->

User-authored `root` remains symbolic here.

## RepoForge graph intent
Parent: `root`
Blocked by: `blocker-a`, `blocker-b`
Relates: `related`
Supersedes: `legacy`

## Delivery checklist
- [ ] child — Child title
"""

    rendered = materialize_issue_graph_body(
        body,
        {
            "root": 232,
            "blocker-a": 233,
            "blocker-b": 234,
            "related": 235,
            "legacy": 236,
            "child": 237,
        },
    )

    assert "<!-- repoforge-issue:source -->" in rendered
    assert "User-authored `root` remains symbolic here." in rendered
    assert "Parent: #232" in rendered
    assert "Blocked by: #233, #234" in rendered
    assert "Relates: #235" in rendered
    assert "Supersedes: #236" in rendered
    assert "- [ ] #237 — Child title" in rendered


def test_executor_materializes_new_mapping_just_in_time_and_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = PublicationProviderIdentity(
        provider="github",
        api_version="2022-11-28",
        media_type="application/vnd.github+json",
        adapter="test",
        capability_hash="7" * 64,
    )
    monkeypatch.setattr(
        RepositoryIssueGraphStepExecutor,
        "provider_identity",
        lambda _self: provider,
    )
    executor = RepositoryIssueGraphStepExecutor.__new__(RepositoryIssueGraphStepExecutor)
    executor.repo_id = "repoforge"
    step = IssueGraphPublicationStep(
        step_id="igstep-" + "a" * 24,
        ordinal=0,
        kind=PublicationStepKind.UPDATE_EPIC,
        source_ref="root",
        target_ref=None,
        title="Root",
        body="""<!-- repoforge-issue:root -->

## RepoForge graph intent

## Delivery checklist
- [ ] child — Child title
""",
        expected_issue_number=232,
    )
    mapping = {"root": 232}

    with pytest.raises(RepoForgeError) as unresolved:
        executor._request(step, mapping)

    assert unresolved.value.code is ErrorCode.PROPOSAL_BLOCKED
    assert unresolved.value.details == {
        "step_id": step.step_id,
        "unresolved_refs": ["child"],
    }

    mapping["child"] = 233
    request = executor._request(step, mapping)
    assert request["body"] is not None
    assert "- [ ] #233 — Child title" in str(request["body"])
    assert "`child`" not in str(request["body"])


def _all_new_plan(*, forward_relates: bool = False):
    root = IssueNodeDraft(
        "root",
        "Root",
        "epic",
        "p0",
        "ready",
        None,
        "## Objective\n\nRoot.\n\n## Acceptance criteria\n\n- [ ] Done.\n",
    )
    child_a = IssueNodeDraft(
        "child-a",
        "Child A",
        "task",
        "p0",
        "ready",
        "root",
        "## Objective\n\nChild A.\n\n## Acceptance criteria\n\n- [ ] Done.\n",
    )
    child_b = IssueNodeDraft(
        "child-b",
        "Child B",
        "task",
        "p0",
        "ready",
        "root",
        "## Objective\n\nChild B.\n\n## Acceptance criteria\n\n- [ ] Done.\n",
    )
    edges = (
        (IssueEdgeDraft("child-a", "child-b", IssueEdgeKind.RELATES),) if forward_relates else ()
    )
    proposal = plan_issue_graph(
        IssueGraphDraft("repoforge", "root", (root, child_a, child_b), edges),
        _identity(),
        live_issues=(),
        created_at="2026-07-22T00:00:00+00:00",
        expires_at="2026-07-23T00:00:00+00:00",
    )
    return build_issue_graph_publication_plan(
        proposal,
        _identity(),
        live_graph=PublicationLiveGraph(nodes=(), snapshot_sha256="3" * 64),
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


def _execute_plan_bodies(plan) -> tuple[dict[str, int], list[str]]:
    mapping = dict(plan.initial_mapping)
    bodies: list[str] = []
    next_issue = 500
    for step in plan.steps:
        if step.body is not None:
            bodies.append(materialize_issue_graph_body(step.body, mapping, step_id=step.step_id))
        if step.kind is PublicationStepKind.CREATE_NODE:
            mapping[step.source_ref] = next_issue
            next_issue += 1
    return mapping, bodies


def test_two_phase_plan_completes_all_new_root_checklist_without_symbolic_leaks() -> None:
    plan = _all_new_plan()

    mapping, sent_bodies = _execute_plan_bodies(plan)

    assert set(mapping) == {"root", "child-a", "child-b"}
    assert tuple(step.kind for step in plan.steps[:3]) == (
        PublicationStepKind.CREATE_NODE,
        PublicationStepKind.CREATE_NODE,
        PublicationStepKind.CREATE_NODE,
    )
    assert plan.steps[-1].kind is PublicationStepKind.UPDATE_EPIC
    assert all("Parent: `" not in body for body in sent_bodies)
    assert all("Relates: `" not in body for body in sent_bodies)
    assert all("- [ ] child-" not in body for body in sent_bodies)
    assert "- [ ] #501 — Child A" in sent_bodies[-1]
    assert "- [ ] #502 — Child B" in sent_bodies[-1]


def test_two_phase_plan_materializes_forward_relates_after_create_phase() -> None:
    plan = _all_new_plan(forward_relates=True)

    mapping, sent_bodies = _execute_plan_bodies(plan)

    assert mapping["child-b"] > mapping["child-a"]
    assert any(f"Relates: #{mapping['child-b']}" in body for body in sent_bodies)
    assert all("Relates: `child-b`" not in body for body in sent_bodies)


def test_materializer_uses_only_final_managed_section_when_user_body_repeats_heading() -> None:
    user_section = """## RepoForge graph intent
Parent: `literal-user-text`
"""
    body = f"""<!-- repoforge-issue:child -->

{user_section}
## RepoForge graph intent
Parent: `root`
"""

    rendered = materialize_issue_graph_body(body, {"root": 232})

    assert user_section in rendered
    assert rendered.endswith("## RepoForge graph intent\nParent: #232\n")


@dataclass
class _RecordingLedger:
    reservations: list[str]

    def reserve(
        self,
        repo_id: str,
        marker: str,
        *,
        count: int,
        now_epoch: float,
        max_in_window: int,
        window_seconds: int,
    ) -> int:
        self.reservations.append(marker)
        return len(self.reservations)


@dataclass
class _RecordingGateway:
    issue: RemoteIssue
    updates: list[str]
    reads: list[str]

    def issue_details(self, _cwd, _issue_number: int) -> RemoteIssue:
        self.reads.append(self.issue.body)
        return self.issue

    def update_issue(self, _cwd, issue_number: int, *, title: str, body: str) -> RemoteIssue:
        self.updates.append(body)
        self.issue = replace(self.issue, issue_number=issue_number, title=title, body=body)
        return self.issue


def test_executor_materializes_before_ledger_and_reconciliation_uses_identical_body(
    forge_env: ForgeEnvironment,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = _RecordingGateway(
        RemoteIssue(240, 100240, "Old", "OPEN", "<!-- repoforge-issue:child -->", "url"),
        [],
        [],
    )
    ledger = _RecordingLedger([])
    ctx = replace(
        forge_env.service.application.context,
        issue_mutations=gateway,
        external_mutations=ledger,
    )
    executor = RepositoryIssueGraphStepExecutor(ctx, "demo")
    monkeypatch.setattr(executor, "_require_policy", lambda _step: None)
    step = IssueGraphPublicationStep(
        step_id="igstep-" + "c" * 24,
        ordinal=0,
        kind=PublicationStepKind.UPDATE_NODE,
        source_ref="child",
        target_ref=None,
        title="Child",
        body="""<!-- repoforge-issue:child -->

## RepoForge graph intent
Parent: `root`
""",
        expected_issue_number=240,
    )

    with pytest.raises(RepoForgeError):
        executor._materialized_step(step, {"child": 240})
    assert ledger.reservations == []
    assert gateway.updates == []

    mapping = {"child": 240, "root": 232}
    materialized = executor._materialized_step(step, mapping)
    executor._mutate(materialized, mapping, IdempotencyEffectBoundary())
    reconciled = executor._authoritative_outcome(materialized, mapping)

    assert ledger.reservations == [f"issue-graph:{step.step_id}"]
    assert gateway.updates == [materialized.body]
    assert gateway.reads[-1] == materialized.body
    assert reconciled is not None
    assert reconciled.reconciled is True


def test_new_plan_format_is_v2_and_missing_persisted_field_decodes_as_v1() -> None:
    plan = _all_new_plan()
    payload = publication_plan_payload(plan)

    assert plan.plan_format_version == 2
    assert payload["plan_format_version"] == 2

    payload.pop("plan_format_version")
    legacy = publication_plan_from_payload(payload)
    assert legacy.plan_format_version == 1


@pytest.mark.parametrize("legacy_shape", ["full_body_create", "interleaved_update"])
def test_legacy_publication_restart_terminalizes_without_external_effects(
    forge_env: ForgeEnvironment,
    tmp_path,
    legacy_shape: str,
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
    payload = publication_payload(accepted)
    payload.pop("plan_format_version", None)
    steps = list(payload["steps"])
    create_index = next(index for index, step in enumerate(steps) if step["kind"] == "create_node")
    update_index = next(
        index
        for index, step in enumerate(steps)
        if step["kind"] == "update_node" and step["source_ref"] == steps[create_index]["source_ref"]
    )
    if legacy_shape == "full_body_create":
        rendered = dict(proposal.rendered_nodes)
        steps[create_index]["body"] = rendered[str(steps[create_index]["source_ref"])]
    else:
        update = steps.pop(update_index)
        steps.insert(create_index, update)
    for ordinal, step in enumerate(steps):
        step["ordinal"] = ordinal
    payload["steps"] = steps
    legacy = publication_from_payload(payload)
    envelope = coordinator.publications.read_publication(accepted.publication_id)
    assert envelope is not None
    coordinator.publications.save_publication(legacy, expected_revision=envelope.revision)

    with pytest.raises(RepoForgeError) as unsafe:
        coordinator.resume(accepted.publication_id, _identity())

    assert unsafe.value.code is ErrorCode.WORKFLOW_REPLAY_UNSAFE
    assert unsafe.value.retryable is False
    assert unsafe.value.safe_next_action == (
        "Create and approve a fresh issue graph publication plan; "
        "the legacy approved effect identity cannot be migrated safely."
    )
    assert executor.calls == []
    terminal = coordinator.publications.read_publication(accepted.publication_id)
    assert terminal is not None
    assert terminal.value.state is PublicationState.MANUAL_RECOVERY_REQUIRED
    operation = forge_env.service.operations.status(accepted.operation_id)
    assert operation.state is OperationState.FAILED
    assert operation.error_code == ErrorCode.WORKFLOW_REPLAY_UNSAFE.value
    receipt = forge_env.service.application.context.effect_receipts.read(accepted.receipt_id)
    assert receipt is not None
    assert receipt.value.state is EffectReceiptState.FAILED_BEFORE_EFFECT
    assert receipt.value.effect_boundary_crossed is False
    assert coordinator.resume(accepted.publication_id, _identity()) == terminal.value
    assert executor.calls == []


def test_legacy_publication_terminalizes_before_stale_identity_validation(
    forge_env: ForgeEnvironment,
    tmp_path,
) -> None:
    executor = ScriptedExecutor([], [], {}, {}, capability_hash="8" * 64)
    coordinator, proposal = _coordinator(forge_env, tmp_path, executor)
    plan = _prepare(coordinator, proposal)
    executor.capability_hash = plan.provider_identity.capability_hash
    accepted = coordinator.accept(
        plan.plan_id,
        approved_proposal_hash=proposal.proposal_hash,
        approved_effect_plan_hash=plan.effect_plan_hash,
        actual_identity=_identity(),
        now="2026-07-22T00:00:00+00:00",
    )
    envelope = coordinator.publications.read_publication(accepted.publication_id)
    assert envelope is not None
    coordinator.publications.save_publication(
        replace(accepted, plan_format_version=1),
        expected_revision=envelope.revision,
    )
    executor.capability_hash = "9" * 64

    with pytest.raises(RepoForgeError) as unsafe:
        coordinator.resume(
            accepted.publication_id,
            _identity(active_generation=13, input_contract_digest="8" * 64),
        )

    assert unsafe.value.code is ErrorCode.WORKFLOW_REPLAY_UNSAFE
    assert executor.calls == []
    terminal = coordinator.publications.read_publication(accepted.publication_id)
    assert terminal is not None
    assert terminal.value.state is PublicationState.MANUAL_RECOVERY_REQUIRED


def test_legacy_publication_with_durable_effect_evidence_never_claims_before_effect(
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
    result_reference = f"issue-graph-step:{accepted.steps[0].step_id}"
    effect_step = replace(
        accepted.steps[0],
        state=PublicationStepState.APPLIED,
        result_reference=result_reference,
        external_writes=1,
    )
    legacy = replace(
        accepted,
        plan_format_version=1,
        steps=(effect_step, *accepted.steps[1:]),
        external_writes=1,
    )
    envelope = coordinator.publications.read_publication(accepted.publication_id)
    assert envelope is not None
    coordinator.publications.save_publication(legacy, expected_revision=envelope.revision)

    with pytest.raises(RepoForgeError) as unsafe:
        coordinator.resume(accepted.publication_id, _identity())

    assert unsafe.value.code is ErrorCode.WORKFLOW_REPLAY_UNSAFE
    receipt_store = forge_env.service.application.context.effect_receipts
    assert receipt_store is not None
    receipt = receipt_store.read(accepted.receipt_id)
    assert receipt is not None
    assert receipt.value.state is EffectReceiptState.FAILED_AFTER_EFFECT
    assert receipt.value.effect_boundary_crossed is True
    assert receipt.value.result_reference == result_reference
    operation = forge_env.service.operations.status(accepted.operation_id)
    assert operation.retryability is OperationRetryability.MANUAL
    assert executor.calls == []


def test_legacy_publication_preserves_an_already_terminal_receipt(
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
    envelope = coordinator.publications.read_publication(accepted.publication_id)
    assert envelope is not None
    coordinator.publications.save_publication(
        replace(accepted, plan_format_version=1),
        expected_revision=envelope.revision,
    )
    receipt_store = forge_env.service.application.context.effect_receipts
    assert receipt_store is not None
    receipt = receipt_store.read(accepted.receipt_id)
    assert receipt is not None
    terminal_receipt = transition_effect_receipt(
        receipt.value,
        EffectReceiptState.UNKNOWN,
        now="2026-07-22T00:00:01+00:00",
        error_code="PREEXISTING_UNKNOWN",
        error_message="Prior durable uncertainty",
        effect_boundary_crossed=True,
    )
    receipt_store.save(terminal_receipt, expected_revision=receipt.revision)

    with pytest.raises(RepoForgeError) as unsafe:
        coordinator.resume(accepted.publication_id, _identity())

    assert unsafe.value.code is ErrorCode.WORKFLOW_REPLAY_UNSAFE
    preserved = receipt_store.read(accepted.receipt_id)
    assert preserved is not None
    assert preserved.value == terminal_receipt
    assert executor.calls == []


def test_legacy_rejection_preserves_terminal_success_evidence(
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
    publication_envelope = coordinator.publications.read_publication(accepted.publication_id)
    assert publication_envelope is not None
    coordinator.publications.save_publication(
        replace(accepted, plan_format_version=1),
        expected_revision=publication_envelope.revision,
    )
    result_reference = f"issue-graph-publication:{accepted.publication_id}"
    receipt_store = forge_env.service.application.context.effect_receipts
    assert receipt_store is not None
    receipt = receipt_store.read(accepted.receipt_id)
    assert receipt is not None
    applied = transition_effect_receipt(
        receipt.value,
        EffectReceiptState.APPLIED_UNVALIDATED,
        now="2026-07-22T00:00:01+00:00",
        result_reference=result_reference,
        effect_boundary_crossed=True,
    )
    validated = transition_effect_receipt(
        applied,
        EffectReceiptState.APPLIED_VALIDATED,
        now="2026-07-22T00:00:02+00:00",
        result_reference=result_reference,
    )
    receipt_store.save(validated, expected_revision=receipt.revision)
    succeeded = coordinator.operations.succeed(
        accepted.operation_id,
        result_reference=result_reference,
        receipt_id=accepted.receipt_id,
        now="2026-07-22T00:00:02+00:00",
    )

    with pytest.raises(RepoForgeError) as unsafe:
        coordinator.resume(accepted.publication_id, _identity())

    assert unsafe.value.code is ErrorCode.WORKFLOW_REPLAY_UNSAFE
    assert unsafe.value.safe_next_action == (
        "Inspect the preserved terminal operation and receipt evidence, then create and approve "
        "a fresh issue graph publication plan."
    )
    preserved_receipt = receipt_store.read(accepted.receipt_id)
    assert preserved_receipt is not None
    assert preserved_receipt.value == validated
    assert coordinator.operations.status(accepted.operation_id) == succeeded
    terminal = coordinator.publications.read_publication(accepted.publication_id)
    assert terminal is not None
    assert terminal.value.state is PublicationState.MANUAL_RECOVERY_REQUIRED
    replay = coordinator.resume(
        accepted.publication_id,
        _identity(active_generation=99, input_contract_digest="9" * 64),
    )
    assert replay == terminal.value
    assert receipt_store.read(accepted.receipt_id) == preserved_receipt
    assert coordinator.operations.status(accepted.operation_id) == succeeded
    assert executor.calls == []


@pytest.mark.parametrize("fault_after", ["receipt", "operation"])
def test_legacy_rejection_resumes_after_intermediate_durable_fault(
    forge_env: ForgeEnvironment,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    fault_after: str,
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
    envelope = coordinator.publications.read_publication(accepted.publication_id)
    assert envelope is not None
    coordinator.publications.save_publication(
        replace(accepted, plan_format_version=1),
        expected_revision=envelope.revision,
    )

    if fault_after == "receipt":
        original_fail = coordinator.operations.fail
        injected = False

        def fail_once(*args, **kwargs):
            nonlocal injected
            if not injected:
                injected = True
                raise RuntimeError("fault after receipt")
            return original_fail(*args, **kwargs)

        monkeypatch.setattr(coordinator.operations, "fail", fail_once)
    else:
        original_save = coordinator.publications.save_publication
        injected = False

        def save_once(*args, **kwargs):
            nonlocal injected
            if not injected:
                injected = True
                raise RuntimeError("fault after operation")
            return original_save(*args, **kwargs)

        monkeypatch.setattr(coordinator.publications, "save_publication", save_once)

    with pytest.raises(RuntimeError, match=f"fault after {fault_after}"):
        coordinator.resume(accepted.publication_id, _identity())

    with pytest.raises(RepoForgeError) as unsafe:
        coordinator.resume(accepted.publication_id, _identity())

    assert unsafe.value.code is ErrorCode.WORKFLOW_REPLAY_UNSAFE
    receipt_store = forge_env.service.application.context.effect_receipts
    assert receipt_store is not None
    receipt = receipt_store.read(accepted.receipt_id)
    assert receipt is not None
    assert receipt.value.state is EffectReceiptState.FAILED_BEFORE_EFFECT
    operation = forge_env.service.operations.status(accepted.operation_id)
    assert operation.state is OperationState.FAILED
    assert operation.error_code == ErrorCode.WORKFLOW_REPLAY_UNSAFE.value
    terminal = coordinator.publications.read_publication(accepted.publication_id)
    assert terminal is not None
    assert terminal.value.state is PublicationState.MANUAL_RECOVERY_REQUIRED
    assert coordinator.resume(accepted.publication_id, _identity()) == terminal.value
    assert executor.calls == []


def test_malformed_v2_create_body_fails_before_any_effect() -> None:
    plan = _all_new_plan()
    create = next(step for step in plan.steps if step.kind is PublicationStepKind.CREATE_NODE)
    malformed = replace(
        create,
        body="""<!-- repoforge-issue:root -->

## RepoForge graph intent

## Delivery checklist
- [ ] child — Child
""",
    )

    with pytest.raises(RepoForgeError) as blocked:
        RepositoryIssueGraphStepExecutor._materialized_step(malformed, {})

    assert blocked.value.code is ErrorCode.PROPOSAL_BLOCKED


def test_unmanaged_root_is_adopted_and_finalized_with_one_provider_write() -> None:
    proposal = _proposal()
    current = _live()
    live = replace(
        current,
        nodes=(
            replace(current.nodes[0], managed=False, body="unmanaged"),
            current.nodes[1],
        ),
    )
    plan = build_issue_graph_publication_plan(
        proposal,
        _identity(),
        live_graph=live,
        adopt_refs=("epic-232", "task-234"),
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

    root_writes = tuple(
        step
        for step in plan.steps
        if step.source_ref == "epic-232"
        and step.kind
        in {
            PublicationStepKind.ADOPT_NODE,
            PublicationStepKind.UPDATE_NODE,
            PublicationStepKind.UPDATE_EPIC,
        }
    )
    assert len(root_writes) == 1
    assert root_writes[0].kind is PublicationStepKind.ADOPT_NODE
    assert root_writes[0] == plan.steps[-1]
