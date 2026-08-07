"""Preview and apply exact-state durable-operation repairs."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime

from ...domain.errors import ErrorCode, RepoForgeError
from ...domain.operation_repair import (
    OperationRepairBlocker,
    OperationRepairDisposition,
    OperationRepairProposal,
    OperationRepairSnapshot,
    operation_repair_proposal,
)
from ...domain.operation_task import TERMINAL_OPERATION_STATES, OperationState, OperationTask
from ...domain.operation_work import OperationWorkItem, OperationWorkState, requeue_unstarted_work
from ...domain.operation_worker import OperationWorkerBinding, worker_binding_payload
from ...ports.operation_work_queue import OperationWorkQueue
from ...ports.process_reaper import ProcessReaper
from ...ports.worker_binding_store import WorkerBindingStore
from .manager import OperationManager


@dataclass(frozen=True, slots=True)
class OperationRepairCommand:
    action: str
    operation_id: str
    proposal_token: str | None = None
    now: str | None = None


@dataclass(frozen=True, slots=True)
class OperationRepairResult:
    proposal: OperationRepairProposal
    applied: bool
    operation: OperationTask


def _timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("operation repair timestamps must include a timezone")
    return parsed


def _binding_digest(binding: OperationWorkerBinding | None) -> str | None:
    if binding is None:
        return None
    encoded = json.dumps(
        worker_binding_payload(binding),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _blocker(code: str, detail: str) -> tuple[OperationRepairBlocker, ...]:
    return (OperationRepairBlocker(code, detail),)


def classify_operation_child_binding(
    work: OperationWorkItem,
    binding: OperationWorkerBinding | None,
) -> tuple[OperationRepairDisposition | None, tuple[OperationRepairBlocker, ...]]:
    """Classify durable child identity without signalling a process."""
    if binding is None:
        return (
            OperationRepairDisposition.BLOCKED_MISSING_BINDING,
            _blocker(
                "missing_binding",
                "The child spawn boundary was crossed but no durable child identity exists.",
            ),
        )
    if binding.owner_id is not None and binding.owner_id != work.owner_id:
        return (
            OperationRepairDisposition.BLOCKED_OWNER_MISMATCH,
            _blocker(
                "owner_mismatch",
                "The durable child binding owner does not match the claimed work owner.",
            ),
        )
    if binding.attempt is not None and binding.attempt != work.attempt:
        return (
            OperationRepairDisposition.BLOCKED_ATTEMPT_MISMATCH,
            _blocker(
                "attempt_mismatch",
                "The durable child binding attempt does not match the claimed work attempt.",
            ),
        )
    return None, ()


class OperationRepairService:
    """Classify repair safety from durable state and apply only exact reviewed proposals."""

    def __init__(
        self,
        operations: OperationManager,
        queue: OperationWorkQueue,
        worker_bindings: WorkerBindingStore,
        reaper: ProcessReaper,
    ) -> None:
        self._operations = operations
        self._queue = queue
        self._worker_bindings = worker_bindings
        self._reaper = reaper

    def _now(self, explicit: str | None) -> str:
        return explicit or self._operations.ctx.clock.now_iso()

    def _snapshot(
        self,
        operation: OperationTask,
        work: OperationWorkItem | None,
        binding: OperationWorkerBinding | None,
    ) -> OperationRepairSnapshot:
        return OperationRepairSnapshot(
            operation_id=operation.operation_id,
            operation_updated_at=operation.updated_at,
            operation_state=operation.state.value,
            operation_owner_id=operation.owner_id,
            operation_attempt=operation.attempt,
            operation_lease_expires_at=operation.lease_expires_at,
            cancellation_requested_at=operation.cancellation_requested_at,
            work_updated_at=None if work is None else work.updated_at,
            work_state=None if work is None else work.state.value,
            work_owner_id=None if work is None else work.owner_id,
            work_attempt=None if work is None else work.attempt,
            work_lease_expires_at=None if work is None else work.lease_expires_at,
            child_started=None if work is None else work.child_started,
            binding_digest=_binding_digest(binding),
        )

    def preview(self, operation_id: str, *, now: str | None = None) -> OperationRepairResult:
        current_now = self._now(now)
        operation = self._operations.status(operation_id)
        work = self._queue.read(operation_id)
        binding = self._worker_bindings.get(operation_id)
        snapshot = self._snapshot(operation, work, binding)

        if operation.state in TERMINAL_OPERATION_STATES:
            proposal = operation_repair_proposal(
                snapshot,
                disposition=OperationRepairDisposition.ALREADY_TERMINAL,
            )
            return OperationRepairResult(proposal, False, operation)
        if work is None:
            proposal = operation_repair_proposal(
                snapshot,
                disposition=OperationRepairDisposition.NOT_REPAIRABLE,
                blockers=_blocker(
                    "missing_work",
                    "The non-terminal operation has no durable work sidecar to repair safely.",
                ),
            )
            return OperationRepairResult(proposal, False, operation)

        disposition: OperationRepairDisposition | None
        if operation.cancellation_requested_at is not None:
            if not work.child_started:
                disposition = OperationRepairDisposition.CANCEL_QUEUED
                blockers: tuple[OperationRepairBlocker, ...] = ()
            else:
                disposition, blockers = classify_operation_child_binding(work, binding)
                if disposition is None:
                    disposition = OperationRepairDisposition.CANCEL_REAPED
            proposal = operation_repair_proposal(
                snapshot,
                disposition=disposition,
                blockers=blockers,
            )
            return OperationRepairResult(proposal, False, operation)

        lease_expired = (
            work.state is OperationWorkState.CLAIMED
            and work.lease_expires_at is not None
            and _timestamp(work.lease_expires_at) <= _timestamp(current_now)
        )
        if lease_expired and not work.child_started:
            proposal = operation_repair_proposal(
                snapshot,
                disposition=OperationRepairDisposition.REQUEUE_UNSTARTED,
            )
            return OperationRepairResult(proposal, False, operation)
        if lease_expired and work.child_started:
            disposition, blockers = classify_operation_child_binding(work, binding)
            if disposition is None:
                disposition = OperationRepairDisposition.ORPHAN_REAPED
            proposal = operation_repair_proposal(
                snapshot,
                disposition=disposition,
                blockers=blockers,
            )
            return OperationRepairResult(proposal, False, operation)

        proposal = operation_repair_proposal(
            snapshot,
            disposition=OperationRepairDisposition.NOT_REPAIRABLE,
            blockers=_blocker(
                "state_not_repairable",
                "The operation is not terminal, cancellation-requested, or an expired claimed item.",
            ),
        )
        return OperationRepairResult(proposal, False, operation)

    def _require_matching_token(
        self,
        command: OperationRepairCommand,
        current: OperationRepairResult,
    ) -> None:
        token = command.proposal_token
        if token is None or token != current.proposal.proposal_token:
            raise RepoForgeError(
                "Operation repair proposal is stale; durable state changed after preview",
                code=ErrorCode.OPERATION_REPAIR_STALE,
                retryable=True,
                safe_next_action="Run operation repair preview again and apply its exact proposal token.",
                unchanged_state=("No operation, work item, binding, or process was changed.",),
                details={
                    "operation_id": command.operation_id,
                    "current_proposal_token": current.proposal.proposal_token,
                },
            )

    def _reap_bound_child(
        self,
        operation_id: str,
    ) -> OperationWorkerBinding:
        binding = self._worker_bindings.get(operation_id)
        if binding is None:
            raise RepoForgeError(
                "Operation repair became blocked because the durable child binding disappeared",
                code=ErrorCode.OPERATION_REPAIR_STALE,
                retryable=True,
            )
        outcome = self._reaper.reap(binding)
        if not outcome.reaped or outcome.still_alive:
            code = "child_survived" if outcome.still_alive else "identity_unproven"
            disposition = (
                OperationRepairDisposition.BLOCKED_CHILD_SURVIVED
                if outcome.still_alive
                else OperationRepairDisposition.BLOCKED_IDENTITY_UNPROVEN
            )
            raise RepoForgeError(
                f"Operation repair is blocked: {outcome.detail}",
                code=ErrorCode.OPERATION_REPAIR_BLOCKED,
                unchanged_state=(
                    "The operation and work item remain non-terminal.",
                    "The durable child binding is preserved for inspection.",
                ),
                safe_next_action=(
                    "Inspect the bound process identity and retry repair only after the child group "
                    "can be proven gone."
                ),
                details={
                    "operation_id": operation_id,
                    "disposition": disposition.value,
                    "blocker_code": code,
                },
            )
        return binding

    def apply(self, command: OperationRepairCommand) -> OperationRepairResult:
        current = self.preview(command.operation_id, now=command.now)
        self._require_matching_token(command, current)
        proposal = current.proposal
        if not proposal.repairable:
            raise RepoForgeError(
                "Operation repair is blocked by unproven or unsupported durable state",
                code=ErrorCode.OPERATION_REPAIR_BLOCKED,
                unchanged_state=("No operation, work item, binding, or process was changed.",),
                safe_next_action="Resolve the reported blockers, then preview the operation again.",
                details={
                    "operation_id": command.operation_id,
                    "disposition": proposal.disposition.value,
                    "blockers": [
                        {"code": blocker.code, "detail": blocker.detail}
                        for blocker in proposal.blockers
                    ],
                },
            )
        if proposal.disposition is OperationRepairDisposition.ALREADY_TERMINAL:
            return OperationRepairResult(proposal, False, current.operation)

        now = self._now(command.now)
        operation_id = command.operation_id
        work = self._queue.read(operation_id)
        if work is None:
            raise RepoForgeError(
                "Operation work changed after repair preview",
                code=ErrorCode.OPERATION_REPAIR_STALE,
                retryable=True,
            )

        def mutate() -> OperationTask:
            if proposal.disposition is OperationRepairDisposition.CANCEL_QUEUED:
                operation = self._operations.cancelled(
                    operation_id,
                    owner_id=current.operation.owner_id,
                    now=now,
                )
                self._queue.delete(operation_id)
                return operation
            if proposal.disposition is OperationRepairDisposition.REQUEUE_UNSTARTED:
                queued = requeue_unstarted_work(work, now=now)
                self._queue.save(queued, expected_updated_at=work.updated_at)
                if current.operation.state is OperationState.RUNNING:
                    return self._operations.requeue(operation_id, now=now)
                return self._operations.status(operation_id)

            binding = self._reap_bound_child(operation_id)
            if proposal.disposition is OperationRepairDisposition.CANCEL_REAPED:
                operation = self._operations.cancelled(
                    operation_id,
                    owner_id=current.operation.owner_id,
                    now=now,
                )
            elif proposal.disposition is OperationRepairDisposition.ORPHAN_REAPED:
                operation = self._operations.orphan(
                    operation_id,
                    error_code="OPERATION_WORKER_LOST",
                    error_message=(
                        "Operation repair proved the expired worker child process group is gone."
                    ),
                    now=now,
                )
            else:  # pragma: no cover - guarded by repairable disposition classification
                raise AssertionError(f"Unsupported repair disposition: {proposal.disposition}")
            self._queue.delete(operation_id)
            self._worker_bindings.delete_if_unchanged(binding)
            return operation

        operation = self._operations.ctx.audited(
            "operation_repair_apply",
            {
                "operation_id": operation_id,
                "disposition": proposal.disposition.value,
                "proposal_token": proposal.proposal_token,
            },
            mutate,
            mutating=True,
        )
        return OperationRepairResult(proposal, True, operation)

    def execute(self, command: OperationRepairCommand) -> OperationRepairResult:
        if command.action == "preview":
            return self.preview(command.operation_id, now=command.now)
        if command.action == "apply":
            return self.apply(command)
        raise RepoForgeError(
            "Operation repair action must be preview or apply",
            code=ErrorCode.OPERATION_INVALID,
        )
