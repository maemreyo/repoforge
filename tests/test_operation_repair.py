from __future__ import annotations

from dataclasses import replace

import pytest
from conftest import TEST_CONFIG_GENERATION, ForgeEnvironment

from repoforge.application.operations.repair import OperationRepairCommand, OperationRepairService
from repoforge.domain.errors import ErrorCode, RepoForgeError
from repoforge.domain.operation_repair import (
    OperationRepairBlocker,
    OperationRepairDisposition,
    OperationRepairSnapshot,
    operation_repair_proposal,
)
from repoforge.domain.operation_task import OperationState
from repoforge.domain.operation_work import (
    OperationWorkRequest,
    claim_work_item,
    mark_work_child_started,
    new_work_item,
)
from repoforge.domain.operation_worker import OperationWorkerBinding
from repoforge.interfaces.cli.main import build_parser
from repoforge.ports.process_reaper import ReapOutcome

_CREATED = "2026-08-06T00:00:00+00:00"
_CLAIMED = "2026-08-06T00:00:01+00:00"
_EXPIRED = "2026-08-06T00:01:00+00:00"
_NOW = "2026-08-06T00:02:00+00:00"
_OWNER = "worker-repair"


class FakeReaper:
    def __init__(self, outcome: ReapOutcome) -> None:
        self.outcome = outcome
        self.reaped: list[OperationWorkerBinding] = []

    def reap(self, binding: OperationWorkerBinding) -> ReapOutcome:
        self.reaped.append(binding)
        return self.outcome

    def read_start_token(self, pid: int) -> str | None:
        return f"token-{pid}"


def _repair_service(
    forge_env: ForgeEnvironment,
    *,
    outcome: ReapOutcome | None = None,
) -> tuple[OperationRepairService, FakeReaper]:
    context = forge_env.service.application.context
    assert context.operation_work_queue is not None
    assert context.worker_bindings is not None
    reaper = FakeReaper(
        outcome
        or ReapOutcome(
            attempted=True,
            reaped=True,
            still_alive=False,
            detail="process group is gone",
        )
    )
    return (
        OperationRepairService(
            forge_env.service.operations,
            context.operation_work_queue,
            context.worker_bindings,
            reaper,
        ),
        reaper,
    )


def _claimed_work(
    forge_env: ForgeEnvironment,
    *,
    child_started: bool,
    cancellation_requested: bool = False,
) -> tuple[str, object]:
    operations = forge_env.service.operations
    queue = forge_env.service.application.context.operation_work_queue
    assert queue is not None
    task = operations.create(
        kind="workspace_run_profile",
        phase="queued",
        cancel_supported=True,
        workspace_id="workspace-repair",
        now=_CREATED,
    )
    item = new_work_item(
        operation_id=task.operation_id,
        request=OperationWorkRequest.profile(
            workspace_id="workspace-repair",
            profile_name="quick",
            expected_head_sha="a" * 40,
            expected_fingerprint="b" * 64,
            config_generation=TEST_CONFIG_GENERATION,
        ),
        now=_CREATED,
    )
    queue.create(item)
    claimed = claim_work_item(
        item,
        owner_id=_OWNER,
        lease_expires_at=_EXPIRED,
        now=_CLAIMED,
    )
    queue.save(claimed, expected_updated_at=item.updated_at)
    operations.start(
        task.operation_id,
        owner_id=_OWNER,
        lease_expires_at=_EXPIRED,
        attempt=claimed.attempt,
        now=_CLAIMED,
    )
    if child_started:
        started = mark_work_child_started(
            claimed,
            owner_id=_OWNER,
            attempt=claimed.attempt,
            now="2026-08-06T00:00:02+00:00",
        )
        queue.save(started, expected_updated_at=claimed.updated_at)
        claimed = started
    if cancellation_requested:
        operations.request_cancel(task.operation_id, now="2026-08-06T00:00:03+00:00")
    return task.operation_id, claimed


def _binding(operation_id: str, *, owner_id: str = _OWNER, attempt: int = 1):
    return OperationWorkerBinding(
        operation_id=operation_id,
        child_pid=4242,
        child_pgid=4242,
        child_start_token="child-token",
        server_pid=4141,
        server_start_token="server-token",
        created_at="2026-08-06T00:00:02+00:00",
        owner_generation=TEST_CONFIG_GENERATION,
        owner_id=owner_id,
        attempt=attempt,
    )


def test_repair_proposal_token_is_canonical_and_blockers_are_sorted() -> None:
    snapshot = OperationRepairSnapshot(
        operation_id="op-0123456789abcdef01234567",
        operation_updated_at="2026-08-06T00:00:00+00:00",
        operation_state="running",
        operation_owner_id=_OWNER,
        operation_attempt=1,
        operation_lease_expires_at=_EXPIRED,
        cancellation_requested_at=None,
        work_updated_at="2026-08-06T00:00:02+00:00",
        work_state="claimed",
        work_owner_id=_OWNER,
        work_attempt=1,
        work_lease_expires_at=_EXPIRED,
        child_started=True,
        binding_digest=None,
    )
    blockers = (
        OperationRepairBlocker("z-last", "last"),
        OperationRepairBlocker("a-first", "first"),
    )

    first = operation_repair_proposal(
        snapshot,
        disposition=OperationRepairDisposition.BLOCKED_MISSING_BINDING,
        blockers=blockers,
    )
    second = operation_repair_proposal(
        snapshot,
        disposition=OperationRepairDisposition.BLOCKED_MISSING_BINDING,
        blockers=tuple(reversed(blockers)),
    )

    assert first == second
    assert [item.code for item in first.blockers] == ["a-first", "z-last"]
    assert len(first.proposal_token) == 64
    assert first.repairable is False


def test_preview_preserves_started_work_without_a_binding_and_apply_is_blocked(
    forge_env: ForgeEnvironment,
) -> None:
    service, reaper = _repair_service(forge_env)
    operation_id, _ = _claimed_work(forge_env, child_started=True)

    preview = service.execute(OperationRepairCommand("preview", operation_id, now=_NOW))

    assert preview.applied is False
    assert preview.proposal.disposition is OperationRepairDisposition.BLOCKED_MISSING_BINDING
    assert preview.proposal.repairable is False
    assert reaper.reaped == []
    with pytest.raises(RepoForgeError) as blocked:
        service.execute(
            OperationRepairCommand(
                "apply",
                operation_id,
                proposal_token=preview.proposal.proposal_token,
                now=_NOW,
            )
        )
    assert blocked.value.code is ErrorCode.OPERATION_REPAIR_BLOCKED
    assert forge_env.service.operations.status(operation_id).state is OperationState.RUNNING


def test_apply_requeues_only_expired_work_that_never_crossed_spawn_boundary(
    forge_env: ForgeEnvironment,
) -> None:
    service, _ = _repair_service(forge_env)
    operation_id, _ = _claimed_work(forge_env, child_started=False)

    preview = service.execute(OperationRepairCommand("preview", operation_id, now=_NOW))
    assert preview.proposal.disposition is OperationRepairDisposition.REQUEUE_UNSTARTED

    repaired = service.execute(
        OperationRepairCommand(
            "apply",
            operation_id,
            proposal_token=preview.proposal.proposal_token,
            now=_NOW,
        )
    )

    queue = forge_env.service.application.context.operation_work_queue
    assert queue is not None
    work = queue.read(operation_id)
    assert repaired.applied is True
    assert repaired.operation.state is OperationState.PENDING
    assert work is not None and work.state.value == "queued"
    assert work.owner_id is None and work.lease_expires_at is None


def test_apply_reaps_a_matching_cancelled_child_before_terminalizing(
    forge_env: ForgeEnvironment,
) -> None:
    service, reaper = _repair_service(forge_env)
    operation_id, _ = _claimed_work(
        forge_env,
        child_started=True,
        cancellation_requested=True,
    )
    bindings = forge_env.service.application.context.worker_bindings
    queue = forge_env.service.application.context.operation_work_queue
    assert bindings is not None and queue is not None
    binding = _binding(operation_id)
    bindings.put(binding)

    preview = service.execute(OperationRepairCommand("preview", operation_id, now=_NOW))
    assert preview.proposal.disposition is OperationRepairDisposition.CANCEL_REAPED

    repaired = service.execute(
        OperationRepairCommand(
            "apply",
            operation_id,
            proposal_token=preview.proposal.proposal_token,
            now=_NOW,
        )
    )

    assert repaired.applied is True
    assert repaired.operation.state is OperationState.CANCELLED
    assert reaper.reaped == [binding]
    assert queue.read(operation_id) is None
    assert bindings.get(operation_id) is None


def test_apply_rejects_a_stale_proposal_before_reaping_or_mutating(
    forge_env: ForgeEnvironment,
) -> None:
    service, reaper = _repair_service(forge_env)
    operation_id, _ = _claimed_work(forge_env, child_started=True)
    bindings = forge_env.service.application.context.worker_bindings
    assert bindings is not None
    bindings.put(_binding(operation_id))
    preview = service.execute(OperationRepairCommand("preview", operation_id, now=_NOW))

    current = forge_env.service.operations.status(operation_id)
    forge_env.service.operations.progress(
        operation_id,
        phase=current.phase,
        current=current.progress_current,
        total=current.progress_total,
        owner_id=_OWNER,
        message="new durable evidence",
        now="2026-08-06T00:02:01+00:00",
    )

    with pytest.raises(RepoForgeError) as stale:
        service.execute(
            OperationRepairCommand(
                "apply",
                operation_id,
                proposal_token=preview.proposal.proposal_token,
                now="2026-08-06T00:02:02+00:00",
            )
        )
    assert stale.value.code is ErrorCode.OPERATION_REPAIR_STALE
    assert reaper.reaped == []
    assert forge_env.service.operations.status(operation_id).state is OperationState.RUNNING


def test_preview_blocks_owner_or_attempt_mismatch_without_signalling(
    forge_env: ForgeEnvironment,
) -> None:
    service, reaper = _repair_service(forge_env)
    operation_id, _ = _claimed_work(forge_env, child_started=True)
    bindings = forge_env.service.application.context.worker_bindings
    assert bindings is not None

    bindings.put(_binding(operation_id, owner_id="other-worker"))
    owner = service.execute(OperationRepairCommand("preview", operation_id, now=_NOW))
    assert owner.proposal.disposition is OperationRepairDisposition.BLOCKED_OWNER_MISMATCH

    bindings.put(replace(_binding(operation_id), attempt=2))
    attempt = service.execute(OperationRepairCommand("preview", operation_id, now=_NOW))
    assert attempt.proposal.disposition is OperationRepairDisposition.BLOCKED_ATTEMPT_MISMATCH
    assert reaper.reaped == []


def test_coding_service_exposes_repair_preview_without_extending_mcp_operation_actions(
    forge_env: ForgeEnvironment,
) -> None:
    operation_id, _ = _claimed_work(forge_env, child_started=True)

    result = forge_env.service.operation_repair("preview", operation_id)

    assert result["applied"] is False
    assert result["proposal"]["disposition"] == "blocked_missing_binding"
    assert result["operation"]["operation_id"] == operation_id


def test_operation_repair_cli_requires_an_exact_apply_token() -> None:
    parser = build_parser()
    operation_id = "op-0123456789abcdef01234567"

    preview = parser.parse_args(["operation", "repair", "preview", operation_id])
    apply = parser.parse_args(
        [
            "operation",
            "repair",
            "apply",
            operation_id,
            "--proposal-token",
            "a" * 64,
        ]
    )

    assert preview.operation_command == "repair"
    assert preview.repair_command == "preview"
    assert preview.operation_id == operation_id
    assert apply.repair_command == "apply"
    assert apply.proposal_token == "a" * 64
