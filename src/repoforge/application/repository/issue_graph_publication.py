"""Durable orchestration for approved issue-graph publication sagas."""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from typing import Any, Protocol

from ...domain.durable_state import StateEnvelope
from ...domain.errors import ConfigError, ErrorCode, RepoForgeError
from ...domain.execution_receipt import (
    EffectReceiptState,
    create_effect_receipt,
    transition_effect_receipt,
)
from ...domain.issue_graph_proposal import IssueGraphIdentity, managed_marker
from ...domain.issue_graph_publication import (
    IssueGraphPublication,
    IssueGraphPublicationPlan,
    IssueGraphPublicationStep,
    PublicationLiveGraph,
    PublicationProviderIdentity,
    PublicationState,
    PublicationStepKind,
    PublicationStepState,
    build_issue_graph_publication_plan,
    require_current_publication_identity,
    update_publication_step,
)
from ...domain.operation_task import OperationRetryability
from ...domain.operations import hash_idempotency_key
from ...ports.issue_graph_proposal_store import IssueGraphProposalStore
from ...ports.issue_graph_publication_store import IssueGraphPublicationStore
from ..context import ApplicationContext
from ..extended_context import external_mutation_ledger, issue_mutation_gateway
from ..idempotency import IdempotencyEffectBoundary, execute_idempotent
from ..operations import OperationManager


class PublicationEffectFailure(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        effect_boundary_crossed: bool,
        operation_id: str | None = None,
        receipt_id: str | None = None,
        result_reference: str | None = None,
    ) -> None:
        super().__init__(message)
        self.effect_boundary_crossed = effect_boundary_crossed
        self.operation_id = operation_id
        self.receipt_id = receipt_id
        self.result_reference = result_reference


class PublicationRateLimited(RuntimeError):
    def __init__(
        self,
        retry_at: str,
        *,
        operation_id: str | None = None,
        receipt_id: str | None = None,
        result_reference: str | None = None,
    ) -> None:
        super().__init__(f"Issue graph publication is rate limited until {retry_at}")
        datetime.fromisoformat(retry_at)
        self.retry_at = retry_at
        self.operation_id = operation_id
        self.receipt_id = receipt_id
        self.result_reference = result_reference


@dataclass(frozen=True, slots=True)
class StepEffectResult:
    state: PublicationStepState
    issue_number: int | None
    operation_id: str
    receipt_id: str
    result_reference: str
    external_writes: int
    provider_identity: PublicationProviderIdentity

    def __post_init__(self) -> None:
        if self.state not in {
            PublicationStepState.APPLIED,
            PublicationStepState.RECONCILED_EXISTING,
        }:
            raise ValueError("publication step result state is not terminal success")
        if self.issue_number is not None and self.issue_number <= 0:
            raise ValueError("publication step result issue number is invalid")
        if self.external_writes < 0 or self.external_writes > 20:
            raise ValueError("publication step result external writes are invalid")


class PublicationStepExecutor(Protocol):
    def provider_identity(self) -> PublicationProviderIdentity: ...

    def apply(
        self,
        step: IssueGraphPublicationStep,
        mapping: dict[str, int],
    ) -> StepEffectResult: ...

    def reconcile(
        self,
        step: IssueGraphPublicationStep,
        mapping: dict[str, int],
    ) -> StepEffectResult | None: ...


@dataclass(frozen=True, slots=True)
class _StepMutationOutcome:
    issue_number: int | None
    external_writes: int
    reconciled: bool


_MANAGED_MARKER = re.compile(r"<!-- repoforge-issue:([A-Za-z0-9][A-Za-z0-9._-]{0,79}) -->")
_NODE_EFFECTS = frozenset(
    {
        PublicationStepKind.CREATE_NODE,
        PublicationStepKind.UPDATE_NODE,
        PublicationStepKind.ADOPT_NODE,
        PublicationStepKind.UPDATE_EPIC,
    }
)
_RELATIONSHIP_EFFECTS = frozenset(
    {
        PublicationStepKind.ADD_SUB_ISSUE,
        PublicationStepKind.REMOVE_SUB_ISSUE,
        PublicationStepKind.ADD_DEPENDENCY,
        PublicationStepKind.REMOVE_DEPENDENCY,
    }
)


class RepositoryIssueGraphStepExecutor:
    """Execute one publication step through shared idempotency and receipt primitives."""

    def __init__(self, ctx: ApplicationContext, repo_id: str) -> None:
        self.ctx = ctx
        self.repo_id = repo_id

    def provider_identity(self) -> PublicationProviderIdentity:
        gateway = issue_mutation_gateway(self.ctx)
        semantic = {
            "provider": "github",
            "api_version": "2022-11-28",
            "media_type": "application/vnd.github+json",
            "adapter": type(gateway).__name__,
            "capabilities": [
                "create_issue",
                "update_issue",
                "sub_issues",
                "blocked_by",
                "add_sub_issue",
                "remove_sub_issue",
                "add_blocked_by",
                "remove_blocked_by",
            ],
        }
        digest = hashlib.sha256(repr(sorted(semantic.items())).encode("utf-8")).hexdigest()
        return PublicationProviderIdentity(
            provider="github",
            api_version="2022-11-28",
            media_type="application/vnd.github+json",
            adapter=type(gateway).__name__,
            capability_hash=digest,
        )

    @staticmethod
    def _resolved_number(
        step: IssueGraphPublicationStep,
        mapping: dict[str, int],
        ref: str,
    ) -> int:
        number = mapping.get(ref)
        if number is None and ref == step.source_ref:
            number = step.expected_issue_number
        if number is None or number <= 0:
            raise RepoForgeError(
                f"Publication reference is unresolved: {ref}",
                code=ErrorCode.PROPOSAL_BLOCKED,
                details={"step_id": step.step_id, "client_ref": ref},
            )
        return number

    @staticmethod
    def _deserialize_outcome(payload: Any) -> _StepMutationOutcome:
        if not isinstance(payload, dict):
            raise ConfigError("Stored issue graph step result is invalid")
        issue_number = payload.get("issue_number")
        external_writes = payload.get("external_writes")
        reconciled = payload.get("reconciled")
        if issue_number is not None and (
            not isinstance(issue_number, int) or isinstance(issue_number, bool) or issue_number <= 0
        ):
            raise ConfigError("Stored issue graph step issue number is invalid")
        if (
            not isinstance(external_writes, int)
            or isinstance(external_writes, bool)
            or not 0 <= external_writes <= 20
            or not isinstance(reconciled, bool)
        ):
            raise ConfigError("Stored issue graph step result fields are invalid")
        return _StepMutationOutcome(issue_number, external_writes, reconciled)

    def _policy_operation(self, step: IssueGraphPublicationStep) -> str:
        if step.kind in _NODE_EFFECTS:
            return "create"
        if step.kind in _RELATIONSHIP_EFFECTS:
            return "link"
        raise RepoForgeError(
            f"Unsupported issue graph publication step: {step.kind.value}",
            code=ErrorCode.PROPOSAL_BLOCKED,
        )

    def _require_policy(self, step: IssueGraphPublicationStep) -> None:
        repo = self.ctx.repo(self.repo_id)
        if repo.read_only or not repo.publish_enabled:
            raise ConfigError("Repository policy does not allow issue graph publication")
        operation = self._policy_operation(step)
        if not repo.issue_writes.allows(operation):
            raise ConfigError(f"Issue graph publication requires repo_issue {operation} capability")

    def _request(
        self,
        step: IssueGraphPublicationStep,
        mapping: dict[str, int],
    ) -> dict[str, object]:
        return {
            "repo_id": self.repo_id,
            "step_id": step.step_id,
            "ordinal": step.ordinal,
            "kind": step.kind.value,
            "source_ref": step.source_ref,
            "source_issue": mapping.get(step.source_ref, step.expected_issue_number),
            "target_ref": step.target_ref,
            "target_issue": mapping.get(step.target_ref) if step.target_ref is not None else None,
            "title": step.title,
            "body": step.body,
            "provider_capability_hash": self.provider_identity().capability_hash,
        }

    @staticmethod
    def _marker_refs(body: str) -> tuple[str, ...]:
        return tuple(sorted(set(_MANAGED_MARKER.findall(body))))

    def _require_owned_content(
        self,
        step: IssueGraphPublicationStep,
        current_body: str,
    ) -> None:
        markers = self._marker_refs(current_body)
        conflicting = tuple(ref for ref in markers if ref != step.source_ref)
        if conflicting:
            raise RepoForgeError(
                "Existing issue is owned by a different RepoForge marker",
                code=ErrorCode.PROPOSAL_BLOCKED,
                details={
                    "step_id": step.step_id,
                    "client_ref": step.source_ref,
                    "conflicting_markers": list(conflicting),
                },
            )
        if step.kind is not PublicationStepKind.ADOPT_NODE and step.source_ref not in markers:
            raise RepoForgeError(
                "Managed issue update requires the exact RepoForge ownership marker",
                code=ErrorCode.PROPOSAL_BLOCKED,
                details={"step_id": step.step_id, "client_ref": step.source_ref},
            )

    def _authoritative_outcome(
        self,
        step: IssueGraphPublicationStep,
        mapping: dict[str, int],
    ) -> _StepMutationOutcome | None:
        gateway = issue_mutation_gateway(self.ctx)
        repo = self.ctx.repo(self.repo_id)
        if step.kind is PublicationStepKind.CREATE_NODE:
            marker = managed_marker(step.source_ref)
            issues, truncated = gateway.recent_issues(repo.path, max_issues=100)
            matches = tuple(item for item in issues if marker in item.body)
            if len(matches) > 1:
                raise RepoForgeError(
                    "Managed marker resolves to multiple GitHub issues",
                    code=ErrorCode.PROPOSAL_BLOCKED,
                    details={
                        "step_id": step.step_id,
                        "issue_numbers": [item.issue_number for item in matches],
                    },
                )
            if matches:
                return _StepMutationOutcome(matches[0].issue_number, 0, True)
            if truncated:
                raise ConfigError(
                    "Issue creation reconciliation is incomplete; refusing a blind retry"
                )
            return None
        if step.kind in {
            PublicationStepKind.UPDATE_NODE,
            PublicationStepKind.ADOPT_NODE,
            PublicationStepKind.UPDATE_EPIC,
        }:
            number = self._resolved_number(step, mapping, step.source_ref)
            current = gateway.issue_details(repo.path, number)
            if current.title == step.title and current.body == step.body:
                return _StepMutationOutcome(number, 0, True)
            return None
        if step.target_ref is None:
            raise RepoForgeError(
                "Relationship publication step is missing target_ref",
                code=ErrorCode.PROPOSAL_BLOCKED,
            )
        source_number = self._resolved_number(step, mapping, step.source_ref)
        target_number = self._resolved_number(step, mapping, step.target_ref)
        if step.kind in {
            PublicationStepKind.ADD_SUB_ISSUE,
            PublicationStepKind.REMOVE_SUB_ISSUE,
        }:
            related, truncated = gateway.sub_issues(repo.path, source_number, max_issues=100)
        else:
            related, truncated = gateway.blocked_by(repo.path, source_number, max_issues=100)
        present = any(item.issue_number == target_number for item in related)
        desired_present = step.kind in {
            PublicationStepKind.ADD_SUB_ISSUE,
            PublicationStepKind.ADD_DEPENDENCY,
        }
        if present is desired_present:
            return _StepMutationOutcome(source_number, 0, True)
        if truncated:
            raise ConfigError(
                "Issue relationship reconciliation is incomplete; refusing a blind retry"
            )
        return None

    def _mutate(
        self,
        step: IssueGraphPublicationStep,
        mapping: dict[str, int],
        boundary: IdempotencyEffectBoundary,
    ) -> _StepMutationOutcome:
        existing = self._authoritative_outcome(step, mapping)
        if existing is not None:
            return existing
        self._require_policy(step)
        repo = self.ctx.repo(self.repo_id)
        policy = repo.issue_writes
        external_mutation_ledger(self.ctx).reserve(
            repo.repo_id,
            f"issue-graph:{step.step_id}",
            count=1,
            now_epoch=self.ctx.now_epoch(),
            max_in_window=policy.max_writes_per_window,
            window_seconds=policy.window_seconds,
        )
        gateway = issue_mutation_gateway(self.ctx)
        if step.kind is PublicationStepKind.CREATE_NODE:
            if step.title is None or step.body is None:
                raise RepoForgeError(
                    "Create-node step lacks rendered title or body",
                    code=ErrorCode.PROPOSAL_BLOCKED,
                )
            boundary.begin()
            affected = gateway.create_issue(repo.path, step.title, step.body)
            outcome = _StepMutationOutcome(affected.issue_number, 1, False)
        elif step.kind in {
            PublicationStepKind.UPDATE_NODE,
            PublicationStepKind.ADOPT_NODE,
            PublicationStepKind.UPDATE_EPIC,
        }:
            number = self._resolved_number(step, mapping, step.source_ref)
            current = gateway.issue_details(repo.path, number)
            self._require_owned_content(step, current.body)
            if step.title is None or step.body is None:
                raise RepoForgeError(
                    "Managed-update step lacks rendered title or body",
                    code=ErrorCode.PROPOSAL_BLOCKED,
                )
            boundary.begin()
            affected = gateway.update_issue(
                repo.path,
                number,
                title=step.title,
                body=step.body,
            )
            outcome = _StepMutationOutcome(affected.issue_number, 1, False)
        else:
            if step.target_ref is None:
                raise RepoForgeError(
                    "Relationship step lacks target_ref",
                    code=ErrorCode.PROPOSAL_BLOCKED,
                )
            source_number = self._resolved_number(step, mapping, step.source_ref)
            target_number = self._resolved_number(step, mapping, step.target_ref)
            target = gateway.issue_details(repo.path, target_number)
            boundary.begin()
            if step.kind is PublicationStepKind.ADD_SUB_ISSUE:
                gateway.add_sub_issue(repo.path, source_number, target.database_id)
            elif step.kind is PublicationStepKind.REMOVE_SUB_ISSUE:
                gateway.remove_sub_issue(repo.path, source_number, target.database_id)
            elif step.kind is PublicationStepKind.ADD_DEPENDENCY:
                gateway.add_blocked_by(repo.path, source_number, target.database_id)
            elif step.kind is PublicationStepKind.REMOVE_DEPENDENCY:
                gateway.remove_blocked_by(repo.path, source_number, target.database_id)
            else:
                raise RepoForgeError(
                    f"Unsupported publication step kind: {step.kind.value}",
                    code=ErrorCode.PROPOSAL_BLOCKED,
                )
            outcome = _StepMutationOutcome(source_number, 1, False)
        boundary.record_result(outcome)
        return outcome

    def _durable_result(
        self,
        action: str,
        key: str,
        outcome: _StepMutationOutcome,
    ) -> StepEffectResult:
        store = self.ctx.idempotency
        if store is None:
            raise ConfigError("Idempotency storage is not configured")
        record = store.load(action, hash_idempotency_key(key))
        if record is None or record.operation_id is None or record.receipt_id is None:
            raise ConfigError("Durable issue graph step outcome identity is missing")
        return StepEffectResult(
            state=(
                PublicationStepState.RECONCILED_EXISTING
                if outcome.reconciled
                else PublicationStepState.APPLIED
            ),
            issue_number=outcome.issue_number,
            operation_id=record.operation_id,
            receipt_id=record.receipt_id,
            result_reference=f"operation-result:{record.operation_id}",
            external_writes=outcome.external_writes,
            provider_identity=self.provider_identity(),
        )

    def _retry_at(self) -> str:
        policy = self.ctx.repo(self.repo_id).issue_writes
        return datetime.fromtimestamp(
            self.ctx.now_epoch() + policy.window_seconds,
            tz=timezone.utc,
        ).isoformat()

    def _receipt_identity(
        self,
        action: str,
        key: str,
    ) -> tuple[str | None, str | None, str | None]:
        receipt_store = self.ctx.effect_receipts
        if receipt_store is None:
            return None, None, None
        page = receipt_store.list_for_idempotency(
            action,
            hash_idempotency_key(key),
            max_records=10,
        )
        if not page.records:
            return None, None, None
        receipt = page.records[0].value
        return receipt.operation_id, receipt.receipt_id, receipt.result_reference

    def _failure(
        self,
        exc: RepoForgeError,
        boundary: IdempotencyEffectBoundary,
        *,
        action: str,
        key: str,
    ) -> PublicationEffectFailure:
        details = exc.details
        stored_operation, stored_receipt, stored_result = self._receipt_identity(action, key)
        return PublicationEffectFailure(
            str(exc),
            effect_boundary_crossed=(
                boundary.started or bool(details.get("effect_boundary_crossed", False))
            ),
            operation_id=(
                str(details["operation_id"])
                if isinstance(details.get("operation_id"), str)
                else stored_operation
            ),
            receipt_id=(
                str(details["receipt_id"])
                if isinstance(details.get("receipt_id"), str)
                else stored_receipt
            ),
            result_reference=(
                str(details["result_reference"])
                if isinstance(details.get("result_reference"), str)
                else stored_result
            ),
        )

    def apply(
        self,
        step: IssueGraphPublicationStep,
        mapping: dict[str, int],
    ) -> StepEffectResult:
        boundary = IdempotencyEffectBoundary()
        request = self._request(step, mapping)
        try:
            outcome = execute_idempotent(
                self.ctx,
                "issue_graph_step",
                step.step_id,
                request,
                lambda: self._mutate(step, mapping, boundary),
                details={
                    "repo_id": self.repo_id,
                    "issue_number": request.get("source_issue"),
                    "target_issue": request.get("target_issue"),
                    "provider_capability_hash": self.provider_identity().capability_hash,
                },
                serialize=asdict,
                deserialize=self._deserialize_outcome,
                effect_boundary=boundary,
                reconcile_uncertain=lambda: self._authoritative_outcome(step, mapping),
            )
        except RepoForgeError as exc:
            failure = self._failure(
                exc,
                boundary,
                action="issue_graph_step",
                key=step.step_id,
            )
            if str(exc).startswith("EXTERNAL_MUTATION_RATE_LIMIT"):
                raise PublicationRateLimited(
                    self._retry_at(),
                    operation_id=failure.operation_id,
                    receipt_id=failure.receipt_id,
                    result_reference=failure.result_reference,
                ) from exc
            raise failure from exc
        return self._durable_result("issue_graph_step", step.step_id, outcome)

    def reconcile(
        self,
        step: IssueGraphPublicationStep,
        mapping: dict[str, int],
    ) -> StepEffectResult | None:
        proof = self._authoritative_outcome(step, mapping)
        if proof is None:
            return None
        key = f"{step.step_id}:reconcile"
        outcome = execute_idempotent(
            self.ctx,
            "issue_graph_step_reconcile",
            key,
            {**self._request(step, mapping), "reconciliation": True},
            lambda: replace(proof, reconciled=True, external_writes=0),
            details={
                "repo_id": self.repo_id,
                "issue_number": proof.issue_number,
                "provider_capability_hash": self.provider_identity().capability_hash,
            },
            serialize=asdict,
            deserialize=self._deserialize_outcome,
        )
        return self._durable_result("issue_graph_step_reconcile", key, outcome)


class IssueGraphPublicationCoordinator:
    def __init__(
        self,
        ctx: ApplicationContext,
        operations: OperationManager,
        proposals: IssueGraphProposalStore,
        publications: IssueGraphPublicationStore,
        executor: PublicationStepExecutor,
    ) -> None:
        self.ctx = ctx
        self.operations = operations
        self.proposals = proposals
        self.publications = publications
        self.executor = executor

    @staticmethod
    def _not_found(kind: str, identity: str) -> RepoForgeError:
        return RepoForgeError(
            f"Issue graph {kind} was not found: {identity}",
            code=ErrorCode.NOT_FOUND,
        )

    def prepare(
        self,
        proposal_id: str,
        actual_identity: IssueGraphIdentity,
        *,
        live_graph: PublicationLiveGraph,
        adopt_refs: tuple[str, ...],
        created_at: str,
        expires_at: str,
    ) -> IssueGraphPublicationPlan:
        proposal_record = self.proposals.read(proposal_id)
        if proposal_record is None:
            raise self._not_found("proposal", proposal_id)
        plan = build_issue_graph_publication_plan(
            proposal_record.value,
            actual_identity,
            live_graph=live_graph,
            adopt_refs=adopt_refs,
            provider_identity=self.executor.provider_identity(),
            created_at=created_at,
            expires_at=expires_at,
        )
        return self.publications.create_plan(plan).value

    def accept(
        self,
        plan_id: str,
        *,
        approved_proposal_hash: str,
        approved_effect_plan_hash: str,
        actual_identity: IssueGraphIdentity,
        now: str | None = None,
    ) -> IssueGraphPublication:
        plan_record = self.publications.read_plan(plan_id)
        if plan_record is None:
            raise self._not_found("publication plan", plan_id)
        plan = plan_record.value
        if (
            approved_proposal_hash != plan.proposal_hash
            or approved_effect_plan_hash != plan.effect_plan_hash
        ):
            raise RepoForgeError(
                "Approved issue graph hashes do not match the immutable publication plan",
                code=ErrorCode.APPROVAL_MISMATCH,
                details={
                    "expected_proposal_hash": plan.proposal_hash,
                    "expected_effect_plan_hash": plan.effect_plan_hash,
                },
            )
        require_current_publication_identity(plan.identity, actual_identity)
        publication_id = f"igpub-{hashlib.sha256(plan.effect_plan_hash.encode()).hexdigest()[:24]}"
        existing = self.publications.read_publication(publication_id)
        if existing is not None:
            return existing.value
        accepted_at = now or self.ctx.clock.now_iso()
        if datetime.fromisoformat(accepted_at) >= datetime.fromisoformat(plan.expires_at):
            raise RepoForgeError(
                "Issue graph publication plan has expired",
                code=ErrorCode.CONFIG_STALE,
                safe_next_action="Create and approve a fresh publication plan.",
            )
        if self.executor.provider_identity() != plan.provider_identity:
            raise RepoForgeError(
                "Issue graph publication provider identity changed after planning",
                code=ErrorCode.CONFIG_STALE,
                safe_next_action="Create and approve a fresh publication plan.",
            )
        receipt_store = self.ctx.effect_receipts
        if receipt_store is None:
            raise RepoForgeError(
                "Durable effect receipt storage is not configured",
                code=ErrorCode.CONFIG_INVALID,
            )
        operation = self.operations.create(
            kind="issue_graph_publication",
            phase="accepted",
            cancel_supported=False,
            expires_at=plan.expires_at,
            now=accepted_at,
        )
        receipt_id = f"receipt-{self.ctx.ids.new_hex(24)}"
        receipt = receipt_store.create(
            create_effect_receipt(
                receipt_id=receipt_id,
                operation_id=operation.operation_id,
                action="issue_graph_publication",
                idempotency_key_hash=plan.proposal_hash,
                request_fingerprint=plan.effect_plan_hash,
                accepted_at=accepted_at,
                correlation_id=publication_id,
                pre_identity={
                    "repo_id": plan.identity.repo_id,
                    "proposal_id": plan.proposal_id,
                    "plan_id": plan.plan_id,
                    "generation": plan.identity.active_generation,
                    "provider_capability_hash": plan.provider_identity.capability_hash,
                },
            )
        )
        applying = receipt_store.save(
            transition_effect_receipt(
                receipt.value,
                EffectReceiptState.APPLYING,
                now=accepted_at,
            ),
            expected_revision=receipt.revision,
        )
        self.operations.start(operation.operation_id, receipt_id=receipt_id, now=accepted_at)
        publication = IssueGraphPublication(
            publication_id=publication_id,
            plan_id=plan.plan_id,
            proposal_id=plan.proposal_id,
            proposal_hash=plan.proposal_hash,
            effect_plan_hash=plan.effect_plan_hash,
            identity=plan.identity,
            provider_identity=plan.provider_identity,
            state=PublicationState.RUNNING,
            steps=plan.steps,
            node_mapping=plan.initial_mapping,
            operation_id=operation.operation_id,
            receipt_id=applying.value.receipt_id,
            result_reference=None,
            retry_at=None,
            external_writes=0,
            created_at=accepted_at,
            updated_at=accepted_at,
            expires_at=plan.expires_at,
        )
        return self.publications.create_publication(publication).value

    @staticmethod
    def _now(publication: IssueGraphPublication, explicit: str | None, fallback: str) -> str:
        return explicit or fallback or publication.updated_at

    @staticmethod
    def _completed(step: IssueGraphPublicationStep) -> bool:
        return step.state in {
            PublicationStepState.APPLIED,
            PublicationStepState.RECONCILED_EXISTING,
        }

    def _save_step(
        self,
        envelope: StateEnvelope[IssueGraphPublication],
        index: int,
        step: IssueGraphPublicationStep,
        *,
        state: PublicationState | None,
        mapping: dict[str, int],
        retry_at: str | None,
        now: str,
    ) -> StateEnvelope[IssueGraphPublication]:
        updated = update_publication_step(
            envelope.value,
            index,
            step,
            state=state,
            mapping=mapping,
            retry_at=retry_at,
            updated_at=now,
        )
        return self.publications.save_publication(updated, expected_revision=envelope.revision)

    def _apply_result(
        self,
        step: IssueGraphPublicationStep,
        result: StepEffectResult,
        mapping: dict[str, int],
        expected_provider: PublicationProviderIdentity,
    ) -> IssueGraphPublicationStep:
        if result.provider_identity != expected_provider:
            raise RepoForgeError(
                "Issue graph publication provider identity changed during execution",
                code=ErrorCode.CONFIG_STALE,
            )
        issue_number = result.issue_number
        if issue_number is not None and step.kind.value.endswith("node"):
            mapping[step.source_ref] = issue_number
        return replace(
            step,
            state=result.state,
            issue_number=issue_number,
            operation_id=result.operation_id,
            receipt_id=result.receipt_id,
            result_reference=result.result_reference,
            external_writes=result.external_writes,
            provider_identity=result.provider_identity,
        )

    def resume(
        self,
        publication_id: str,
        actual_identity: IssueGraphIdentity,
        *,
        now: str | None = None,
    ) -> IssueGraphPublication:
        envelope = self.publications.read_publication(publication_id)
        if envelope is None:
            raise self._not_found("publication", publication_id)
        publication = envelope.value
        require_current_publication_identity(publication.identity, actual_identity)
        if publication.state in {
            PublicationState.SUCCEEDED,
            PublicationState.MANUAL_RECOVERY_REQUIRED,
        }:
            return publication
        if self.executor.provider_identity() != publication.provider_identity:
            raise RepoForgeError(
                "Issue graph publication provider identity changed during execution",
                code=ErrorCode.CONFIG_STALE,
                safe_next_action="Inspect the durable publication and create a fresh plan.",
            )
        current_time = self._now(publication, now, self.ctx.clock.now_iso())
        if publication.state is PublicationState.PAUSED and publication.retry_at is not None:
            if datetime.fromisoformat(current_time) < datetime.fromisoformat(publication.retry_at):
                return publication
            publication = replace(
                publication,
                state=PublicationState.RUNNING,
                retry_at=None,
                updated_at=current_time,
            )
            envelope = self.publications.save_publication(
                publication,
                expected_revision=envelope.revision,
            )
        mapping = dict(envelope.value.node_mapping)
        for index, step in enumerate(envelope.value.steps):
            if self._completed(step):
                continue
            try:
                if step.state is PublicationStepState.FAILED_AFTER_EFFECT:
                    result = self.executor.reconcile(step, mapping)
                    if result is None:
                        manual = replace(
                            step,
                            state=PublicationStepState.MANUAL_RECOVERY_REQUIRED,
                        )
                        envelope = self._save_step(
                            envelope,
                            index,
                            manual,
                            state=PublicationState.MANUAL_RECOVERY_REQUIRED,
                            mapping=mapping,
                            retry_at=None,
                            now=current_time,
                        )
                        receipt_store = self.ctx.effect_receipts
                        if receipt_store is None:
                            raise RepoForgeError(
                                "Durable publication receipt storage is not configured",
                                code=ErrorCode.CONFIG_INVALID,
                            )
                        receipt = receipt_store.read(envelope.value.receipt_id)
                        if receipt is None:
                            raise RepoForgeError(
                                "Issue graph publication acceptance receipt is missing",
                                code=ErrorCode.STATE_CORRUPT,
                            )
                        unknown = receipt_store.save(
                            transition_effect_receipt(
                                receipt.value,
                                EffectReceiptState.UNKNOWN,
                                now=current_time,
                                error_code=ErrorCode.EFFECT_OUTCOME_UNKNOWN.value,
                                error_message=(
                                    "authoritative reconciliation did not confirm the effect"
                                ),
                                effect_boundary_crossed=True,
                            ),
                            expected_revision=receipt.revision,
                        )
                        self.operations.fail(
                            envelope.value.operation_id,
                            error_code=ErrorCode.EFFECT_OUTCOME_UNKNOWN.value,
                            error_message=(
                                "Manual recovery is required for an unconfirmed GitHub effect"
                            ),
                            receipt_id=unknown.value.receipt_id,
                            retryability=OperationRetryability.MANUAL,
                            now=current_time,
                        )
                        return envelope.value
                else:
                    result = self.executor.apply(step, mapping)
            except PublicationRateLimited as exc:
                paused = replace(
                    step,
                    state=PublicationStepState.PAUSED_RATE_LIMIT,
                    operation_id=exc.operation_id or step.operation_id,
                    receipt_id=exc.receipt_id or step.receipt_id,
                    result_reference=exc.result_reference or step.result_reference,
                )
                envelope = self._save_step(
                    envelope,
                    index,
                    paused,
                    state=PublicationState.PAUSED,
                    mapping=mapping,
                    retry_at=exc.retry_at,
                    now=current_time,
                )
                return envelope.value
            except PublicationEffectFailure as exc:
                failed = replace(
                    step,
                    state=(
                        PublicationStepState.FAILED_AFTER_EFFECT
                        if exc.effect_boundary_crossed
                        else PublicationStepState.FAILED_BEFORE_EFFECT
                    ),
                    operation_id=exc.operation_id or step.operation_id,
                    receipt_id=exc.receipt_id or step.receipt_id,
                    result_reference=exc.result_reference or step.result_reference,
                )
                envelope = self._save_step(
                    envelope,
                    index,
                    failed,
                    state=PublicationState.RUNNING,
                    mapping=mapping,
                    retry_at=None,
                    now=current_time,
                )
                return envelope.value
            applied = self._apply_result(
                step,
                result,
                mapping,
                envelope.value.provider_identity,
            )
            envelope = self._save_step(
                envelope,
                index,
                applied,
                state=PublicationState.RUNNING,
                mapping=mapping,
                retry_at=None,
                now=current_time,
            )
            completed = sum(1 for item in envelope.value.steps if self._completed(item))
            self.operations.progress(
                envelope.value.operation_id,
                phase="publishing",
                current=completed,
                total=len(envelope.value.steps),
                unit="step",
                message=f"Published {completed}/{len(envelope.value.steps)} issue-graph steps",
                now=current_time,
            )

        publication = envelope.value
        result_reference = f"issue-graph-publication:{publication.publication_id}"
        result_store = self.ctx.operation_result_store
        receipt_store = self.ctx.effect_receipts
        if result_store is None or receipt_store is None:
            raise RepoForgeError(
                "Durable publication result storage is not configured",
                code=ErrorCode.CONFIG_INVALID,
            )
        result_store.save(
            publication.operation_id,
            {
                "publication_id": publication.publication_id,
                "proposal_id": publication.proposal_id,
                "plan_id": publication.plan_id,
                "node_mapping": dict(publication.node_mapping),
                "steps": [
                    {
                        "step_id": step.step_id,
                        "state": step.state.value,
                        "operation_id": step.operation_id,
                        "receipt_id": step.receipt_id,
                        "result_reference": step.result_reference,
                    }
                    for step in publication.steps
                ],
                "provider_identity": asdict(publication.provider_identity),
            },
        )
        receipt = receipt_store.read(publication.receipt_id)
        if receipt is None:
            raise RepoForgeError(
                "Issue graph publication acceptance receipt is missing",
                code=ErrorCode.STATE_CORRUPT,
            )
        unvalidated = receipt_store.save(
            transition_effect_receipt(
                receipt.value,
                EffectReceiptState.APPLIED_UNVALIDATED,
                now=current_time,
                result_reference=result_reference,
                effect_boundary_crossed=publication.external_writes > 0,
                post_identity={
                    "repo_id": publication.identity.repo_id,
                    "publication_id": publication.publication_id,
                    "provider_capability_hash": publication.provider_identity.capability_hash,
                },
            ),
            expected_revision=receipt.revision,
        )
        validated = receipt_store.save(
            transition_effect_receipt(
                unvalidated.value,
                EffectReceiptState.APPLIED_VALIDATED,
                now=current_time,
                result_reference=result_reference,
                effect_boundary_crossed=publication.external_writes > 0,
            ),
            expected_revision=unvalidated.revision,
        )
        self.operations.succeed(
            publication.operation_id,
            result_reference=result_reference,
            receipt_id=validated.value.receipt_id,
            now=current_time,
        )
        completed_publication = replace(
            publication,
            state=PublicationState.SUCCEEDED,
            result_reference=result_reference,
            retry_at=None,
            updated_at=current_time,
        )
        return self.publications.save_publication(
            completed_publication,
            expected_revision=envelope.revision,
        ).value


__all__ = [
    "IssueGraphPublicationCoordinator",
    "PublicationEffectFailure",
    "PublicationRateLimited",
    "PublicationStepExecutor",
    "RepositoryIssueGraphStepExecutor",
    "StepEffectResult",
]
