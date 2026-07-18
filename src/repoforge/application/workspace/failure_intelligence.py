"""Build, persist, and read normalized execution failure intelligence."""

from __future__ import annotations

from dataclasses import dataclass, replace

from ...domain.egress import (
    EgressContentClass,
    EgressDestination,
    EgressPolicy,
    sanitize_egress_data,
)
from ...domain.errors import ErrorCode, RepoForgeError
from ...domain.execution_plan import ExecutionPlan, PlanStage
from ...domain.execution_receipt import StageReceipt, StageReceiptStatus, WorkspaceIdentity
from ...domain.failure_intelligence import (
    FailureEvidence,
    FailureHistorySignal,
    FailureObservation,
    build_failure_evidence,
    failure_compatibility_binding,
    failure_evidence_payload,
)
from ...ports.failure_evidence_store import FailureEvidenceStore
from ..context import ApplicationContext


@dataclass(frozen=True, slots=True)
class FailureEvidenceReadCommand:
    failure_id: str


class FailureIntelligenceService:
    def __init__(self, ctx: ApplicationContext) -> None:
        self.ctx = ctx

    def _store(self) -> FailureEvidenceStore:
        if self.ctx.failure_evidence is None:
            raise RepoForgeError(
                "Failure evidence store is unavailable",
                code=ErrorCode.CONFIG_INVALID,
            )
        return self.ctx.failure_evidence

    @staticmethod
    def _error_details(exc: Exception) -> dict[str, object]:
        value = getattr(exc, "details", None)
        if not isinstance(value, dict):
            return {}
        return {str(key): item for key, item in list(value.items())[:200]}

    @staticmethod
    def _error_code(exc: Exception) -> str | None:
        raw = getattr(getattr(exc, "code", None), "value", getattr(exc, "code", None))
        return str(raw) if isinstance(raw, str) else None

    @staticmethod
    def _history(
        receipts: tuple[StageReceipt, ...],
        *,
        plan: ExecutionPlan,
        stage: PlanStage,
        pre_identity: WorkspaceIdentity,
        environment_identity: str | None,
        binding_hash: str,
    ) -> tuple[FailureHistorySignal, ...]:
        signals: list[FailureHistorySignal] = []
        for receipt in receipts:
            compatible = (
                receipt.plan_hash == plan.plan_hash
                and receipt.stage_id == stage.stage_id
                and receipt.pre_identity == pre_identity
                and receipt.environment_identity == environment_identity
            )
            receipt_binding = binding_hash if compatible else receipt.target_identity
            outcome = "succeeded" if receipt.status is StageReceiptStatus.SUCCEEDED else "failed"
            signals.append(FailureHistorySignal(receipt_binding, outcome))
        return tuple(signals[-100:])

    def build(
        self,
        *,
        operation_id: str,
        plan: ExecutionPlan,
        stage: PlanStage,
        exc: Exception,
        pre_identity: WorkspaceIdentity,
        post_identity: WorkspaceIdentity,
        environment_identity: str | None,
        changed_paths: tuple[str, ...],
        prior_receipts: tuple[StageReceipt, ...],
    ) -> FailureEvidence:
        details = self._error_details(exc)
        domain = details.get("failure_domain")
        observation_without_history = FailureObservation(
            operation_id=operation_id,
            plan_id=plan.plan_id,
            plan_hash=plan.plan_hash,
            stage_id=stage.stage_id,
            stage_kind=stage.kind.value,
            target=stage.target,
            pre_identity=pre_identity,
            post_identity=post_identity,
            environment_identity=environment_identity,
            error_code=self._error_code(exc),
            message=str(exc) or type(exc).__name__,
            details=details,
            failure_domain=str(domain) if isinstance(domain, str) else None,
            changed_paths=changed_paths,
            history=(),
        )
        binding = failure_compatibility_binding(observation_without_history)
        history = self._history(
            prior_receipts,
            plan=plan,
            stage=stage,
            pre_identity=pre_identity,
            environment_identity=environment_identity,
            binding_hash=binding,
        )
        observation = replace(
            observation_without_history,
            history=history,
            compatibility_binding=binding,
        )
        return build_failure_evidence(
            observation,
            created_at=self.ctx.clock.now_iso(),
        )

    def persist_for_workspace(
        self,
        evidence: FailureEvidence,
        *,
        receipt_id: str,
        workspace_id: str,
    ) -> FailureEvidence:
        finalized = replace(evidence, receipt_id=receipt_id)
        stored = self._store().create(finalized)
        record = self.ctx.store.load(workspace_id)
        raw_ids = record.metadata.get("failure_evidence_ids", ())
        identifiers = [str(item) for item in raw_ids] if isinstance(raw_ids, (list, tuple)) else []
        identifiers = [item for item in identifiers if item != stored.failure_id]
        identifiers.append(stored.failure_id)
        record.metadata["failure_evidence_ids"] = identifiers[-20:]
        record.metadata["last_failure_evidence_id"] = stored.failure_id
        self.ctx.store.save(record)
        return stored

    def read(self, command: FailureEvidenceReadCommand) -> dict[str, object]:
        def operation() -> dict[str, object]:
            evidence = self._store().read(command.failure_id)
            if evidence is None:
                raise RepoForgeError(
                    f"Failure evidence not found: {command.failure_id}",
                    code=ErrorCode.STATE_NOT_FOUND,
                    safe_next_action="Use the exact failure_id returned by operation_status or workspace_status.",
                )
            sanitized = sanitize_egress_data(
                failure_evidence_payload(evidence),
                destination=EgressDestination.MODEL,
                content_class=EgressContentClass.DIAGNOSTIC,
                policy=EgressPolicy(
                    max_input_bytes=256_000,
                    max_output_chars=16_000,
                    max_output_lines=500,
                    withhold_private_keys=True,
                ),
            )
            if not isinstance(sanitized, dict):
                raise RepoForgeError(
                    "Failure evidence could not be serialized safely",
                    code=ErrorCode.EVIDENCE_CORRUPT,
                )
            return {str(key): value for key, value in sanitized.items()}

        return self.ctx.audited(
            "failure_evidence_read",
            {"failure_id": command.failure_id},
            operation,
            mutating=False,
        )
