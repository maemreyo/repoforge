from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from conftest import ForgeEnvironment

from repoforge.application.repository.issue_graph_publication import (
    PublicationEffectFailure,
    PublicationRateLimited,
    RepositoryIssueGraphStepExecutor,
)
from repoforge.domain.errors import ConfigError
from repoforge.domain.execution_receipt import EffectReceiptState
from repoforge.domain.issue_graph_proposal import managed_marker
from repoforge.domain.issue_graph_publication import (
    IssueGraphPublicationStep,
    PublicationStepKind,
    PublicationStepState,
)
from repoforge.domain.issue_writes import IssueWritePolicy
from repoforge.domain.operation_task import OperationState
from repoforge.domain.operations import hash_idempotency_key
from repoforge.ports.issue_mutation import RemoteComment, RemoteIssue


class AlwaysRateLimitedLedger:
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
        del repo_id, marker, count, now_epoch, max_in_window, window_seconds
        raise ConfigError("EXTERNAL_MUTATION_RATE_LIMIT: test window is full")


class MemoryIssueGateway:
    def __init__(self) -> None:
        self.issues: dict[int, RemoteIssue] = {}
        self.sub_issue_numbers: dict[int, set[int]] = {}
        self.blocker_numbers: dict[int, set[int]] = {}
        self.create_calls = 0
        self.update_calls = 0
        self.relationship_writes = 0
        self.lose_create_response = False
        self._next = 300

    @staticmethod
    def _database_id(number: int) -> int:
        return 100_000 + number

    def issue_details(self, cwd: Path, issue_number: int) -> RemoteIssue:
        del cwd
        return self.issues[issue_number]

    def issue_comments(
        self, cwd: Path, issue_number: int, *, max_comments: int
    ) -> tuple[tuple[RemoteComment, ...], bool]:
        del cwd, issue_number, max_comments
        return (), False

    def recent_issues(self, cwd: Path, *, max_issues: int) -> tuple[tuple[RemoteIssue, ...], bool]:
        del cwd
        ordered = tuple(
            sorted(self.issues.values(), key=lambda item: item.issue_number, reverse=True)
        )
        return ordered[:max_issues], len(ordered) > max_issues

    def issue_comment(self, cwd: Path, issue_number: int, body: str) -> RemoteComment:
        del cwd, issue_number, body
        raise AssertionError("publication executor does not use comments")

    def set_issue_state(self, cwd: Path, issue_number: int, state: str) -> RemoteIssue:
        del cwd, issue_number, state
        raise AssertionError("publication executor does not change issue state")

    def create_issue(self, cwd: Path, title: str, body: str) -> RemoteIssue:
        del cwd
        self.create_calls += 1
        number = self._next
        self._next += 1
        issue = RemoteIssue(
            number,
            self._database_id(number),
            title,
            "open",
            body,
            f"https://github.test/issues/{number}",
        )
        self.issues[number] = issue
        if self.lose_create_response:
            self.lose_create_response = False
            raise RuntimeError("response lost after create")
        return issue

    def update_issue(self, cwd: Path, issue_number: int, *, title: str, body: str) -> RemoteIssue:
        del cwd
        self.update_calls += 1
        current = self.issues[issue_number]
        updated = replace(current, title=title, body=body)
        self.issues[issue_number] = updated
        return updated

    def sub_issues(
        self, cwd: Path, issue_number: int, *, max_issues: int
    ) -> tuple[tuple[RemoteIssue, ...], bool]:
        del cwd
        numbers = sorted(self.sub_issue_numbers.get(issue_number, set()))
        return tuple(self.issues[number] for number in numbers[:max_issues]), len(
            numbers
        ) > max_issues

    def blocked_by(
        self, cwd: Path, issue_number: int, *, max_issues: int
    ) -> tuple[tuple[RemoteIssue, ...], bool]:
        del cwd
        numbers = sorted(self.blocker_numbers.get(issue_number, set()))
        return tuple(self.issues[number] for number in numbers[:max_issues]), len(
            numbers
        ) > max_issues

    def add_sub_issue(self, cwd: Path, issue_number: int, sub_issue_id: int) -> RemoteIssue:
        del cwd
        self.relationship_writes += 1
        child = next(item for item in self.issues.values() if item.database_id == sub_issue_id)
        self.sub_issue_numbers.setdefault(issue_number, set()).add(child.issue_number)
        return child

    def add_blocked_by(self, cwd: Path, issue_number: int, blocker_issue_id: int) -> RemoteIssue:
        del cwd
        self.relationship_writes += 1
        blocker = next(
            item for item in self.issues.values() if item.database_id == blocker_issue_id
        )
        self.blocker_numbers.setdefault(issue_number, set()).add(blocker.issue_number)
        return blocker

    def remove_sub_issue(self, cwd: Path, issue_number: int, sub_issue_id: int) -> RemoteIssue:
        del cwd
        self.relationship_writes += 1
        child = next(item for item in self.issues.values() if item.database_id == sub_issue_id)
        self.sub_issue_numbers.setdefault(issue_number, set()).discard(child.issue_number)
        return self.issues[issue_number]

    def remove_blocked_by(self, cwd: Path, issue_number: int, blocker_issue_id: int) -> RemoteIssue:
        del cwd
        self.relationship_writes += 1
        blocker = next(
            item for item in self.issues.values() if item.database_id == blocker_issue_id
        )
        self.blocker_numbers.setdefault(issue_number, set()).discard(blocker.issue_number)
        return self.issues[issue_number]


def _executor(
    forge_env: ForgeEnvironment,
    gateway: MemoryIssueGateway,
    *,
    external_mutations=None,
) -> RepositoryIssueGraphStepExecutor:
    original = forge_env.service.application.context
    repo = replace(
        original.config.repositories["demo"],
        issue_writes=IssueWritePolicy(
            enabled_ops=("link", "create"),
            max_writes_per_call=2,
            max_writes_per_window=20,
            window_seconds=60,
        ),
    )
    config = replace(original.config, repositories={**original.config.repositories, "demo": repo})
    context = replace(
        original,
        config=config,
        issue_mutations=gateway,
        external_mutations=(
            original.external_mutations if external_mutations is None else external_mutations
        ),
    )
    return RepositoryIssueGraphStepExecutor(context, "demo")


def _create_step() -> IssueGraphPublicationStep:
    ref = "task-300"
    return IssueGraphPublicationStep(
        step_id="igstep-" + "a" * 24,
        ordinal=0,
        kind=PublicationStepKind.CREATE_NODE,
        source_ref=ref,
        target_ref=None,
        title="Task 300",
        body=f"{managed_marker(ref)}\n\n## Objective\n\nCreate it.\n",
        expected_issue_number=None,
    )


def test_production_step_executor_is_idempotent_and_returns_durable_receipt(
    forge_env: ForgeEnvironment,
) -> None:
    gateway = MemoryIssueGateway()
    executor = _executor(forge_env, gateway)
    step = _create_step()

    first = executor.apply(step, {})
    replay = executor.apply(step, {})

    assert first == replay
    assert first.state is PublicationStepState.APPLIED
    assert gateway.create_calls == 1
    context = executor.ctx
    assert context.idempotency is not None
    record = context.idempotency.load("issue_graph_step", hash_idempotency_key(step.step_id))
    assert record is not None
    assert record.operation_id == first.operation_id
    assert record.receipt_id == first.receipt_id
    assert context.effect_receipts is not None
    receipt = context.effect_receipts.read(first.receipt_id)
    assert receipt is not None
    assert receipt.value.state is EffectReceiptState.APPLIED_VALIDATED
    assert context.operation_store is not None
    assert context.operation_store.read(first.operation_id).state is OperationState.SUCCEEDED


def test_lost_response_reconciles_through_a_separate_read_receipt(
    forge_env: ForgeEnvironment,
) -> None:
    gateway = MemoryIssueGateway()
    gateway.lose_create_response = True
    executor = _executor(forge_env, gateway)
    step = _create_step()

    with pytest.raises(PublicationEffectFailure) as uncertain:
        executor.apply(step, {})
    assert uncertain.value.effect_boundary_crossed is True
    assert uncertain.value.operation_id is not None
    assert uncertain.value.receipt_id is not None

    reconciled = executor.reconcile(step, {})

    assert reconciled is not None
    assert reconciled.state is PublicationStepState.RECONCILED_EXISTING
    assert gateway.create_calls == 1
    assert reconciled.operation_id != uncertain.value.operation_id
    assert reconciled.receipt_id != uncertain.value.receipt_id
    context = executor.ctx
    assert context.effect_receipts is not None
    original = context.effect_receipts.read(uncertain.value.receipt_id)
    proof = context.effect_receipts.read(reconciled.receipt_id)
    assert original is not None and original.value.state is EffectReceiptState.UNKNOWN
    assert proof is not None and proof.value.state is EffectReceiptState.APPLIED_VALIDATED


def test_marker_conflict_fails_before_effect_boundary(forge_env: ForgeEnvironment) -> None:
    gateway = MemoryIssueGateway()
    gateway.issues[232] = RemoteIssue(
        232,
        gateway._database_id(232),
        "Existing",
        "open",
        f"{managed_marker('other-ref')}\n\nExisting body",
        "https://github.test/issues/232",
    )
    executor = _executor(forge_env, gateway)
    step = IssueGraphPublicationStep(
        step_id="igstep-" + "b" * 24,
        ordinal=0,
        kind=PublicationStepKind.UPDATE_NODE,
        source_ref="task-232",
        target_ref=None,
        title="Updated",
        body=f"{managed_marker('task-232')}\n\nUpdated body",
        expected_issue_number=232,
    )

    with pytest.raises(PublicationEffectFailure) as failure:
        executor.apply(step, {"task-232": 232})

    assert failure.value.effect_boundary_crossed is False
    assert failure.value.operation_id is not None
    assert failure.value.receipt_id is not None
    assert gateway.update_calls == 0
    receipt_store = executor.ctx.effect_receipts
    assert receipt_store is not None
    receipt = receipt_store.read(failure.value.receipt_id)
    assert receipt is not None
    assert receipt.value.state is EffectReceiptState.FAILED_BEFORE_EFFECT


def test_relationship_steps_are_idempotent_and_remove_authoritatively(
    forge_env: ForgeEnvironment,
) -> None:
    gateway = MemoryIssueGateway()
    gateway.issues[10] = RemoteIssue(
        10,
        gateway._database_id(10),
        "Parent",
        "open",
        f"{managed_marker('parent')}\n",
        "https://github.test/issues/10",
    )
    gateway.issues[20] = RemoteIssue(
        20,
        gateway._database_id(20),
        "Child",
        "open",
        f"{managed_marker('child')}\n",
        "https://github.test/issues/20",
    )
    executor = _executor(forge_env, gateway)
    mapping = {"parent": 10, "child": 20}
    add = IssueGraphPublicationStep(
        step_id="igstep-" + "c" * 24,
        ordinal=0,
        kind=PublicationStepKind.ADD_SUB_ISSUE,
        source_ref="parent",
        target_ref="child",
        title=None,
        body=None,
        expected_issue_number=10,
    )
    remove = IssueGraphPublicationStep(
        step_id="igstep-" + "d" * 24,
        ordinal=1,
        kind=PublicationStepKind.REMOVE_SUB_ISSUE,
        source_ref="parent",
        target_ref="child",
        title=None,
        body=None,
        expected_issue_number=10,
    )

    first = executor.apply(add, mapping)
    replay = executor.apply(add, mapping)
    removed = executor.apply(remove, mapping)

    assert first == replay
    assert first.issue_number == 10
    assert removed.issue_number == 10
    assert gateway.relationship_writes == 2
    assert gateway.sub_issue_numbers[10] == set()


def test_rate_limit_exposes_the_durable_failed_before_effect_receipt(
    forge_env: ForgeEnvironment,
) -> None:
    gateway = MemoryIssueGateway()
    executor = _executor(
        forge_env,
        gateway,
        external_mutations=AlwaysRateLimitedLedger(),
    )
    step = _create_step()

    with pytest.raises(PublicationRateLimited) as paused:
        executor.apply(step, {})

    assert paused.value.operation_id is not None
    assert paused.value.receipt_id is not None
    receipt_store = executor.ctx.effect_receipts
    assert receipt_store is not None
    receipt = receipt_store.read(paused.value.receipt_id)
    assert receipt is not None
    assert receipt.value.state is EffectReceiptState.FAILED_BEFORE_EFFECT
