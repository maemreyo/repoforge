"""Build, persist, and read normalized execution failure intelligence."""

from __future__ import annotations

import hashlib
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
    ChangedPathHash,
    FailureEvidence,
    FailureHistorySignal,
    FailureObservation,
    build_failure_evidence,
    failure_compatibility_binding,
    failure_evidence_payload,
)
from ...domain.policy import normalize_relative_path
from ...domain.workspace import WorkspaceRecord
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

    def _changed_path_hashes(
        self, workspace_id: str, changed_paths: tuple[str, ...]
    ) -> tuple[ChangedPathHash, ...]:
        """Real current-content SHA-256 per `changed_path`, `None` where confirmed absent.

        Best-effort: this runs from inside a failure-handling path, so any error here
        (workspace gone, path unreadable, race with a concurrent mutation) must fall back
        to an unknown hash rather than replace the real failure with a hashing failure.
        Deliberately does not gate through `assert_path_allowed`'s allowed/denied_paths
        policy -- `changed_paths` for UNEXPECTED_MUTATION failures names exactly the paths
        that fell outside the accepted change set, so policy-scoped path filtering would
        blind this to the paths that matter most; only workspace-root containment is
        enforced here, mirroring what the eventual restore call itself re-checks.
        """
        if not changed_paths:
            return ()
        try:
            _, _, workspace_root = self.ctx.workspace(workspace_id)
            root = workspace_root.resolve(strict=True)
        except Exception:
            return ()
        hashes: list[ChangedPathHash] = []
        for path in changed_paths:
            sha256: str | None = None
            try:
                normalized = normalize_relative_path(path)
                candidate = (root / normalized).resolve(strict=False)
                candidate.relative_to(root)
                if (
                    self.ctx.filesystem.is_file(candidate)
                    and not self.ctx.filesystem.is_symlink(candidate)
                    and self.ctx.filesystem.size(candidate) <= self.ctx.config.server.max_file_bytes
                ):
                    sha256 = hashlib.sha256(self.ctx.filesystem.read_bytes(candidate)).hexdigest()
            except Exception:
                sha256 = None
            hashes.append(ChangedPathHash(path=path, sha256=sha256))
        return tuple(hashes)

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
            workspace_id=plan.workspace_id,
            pre_identity=pre_identity,
            post_identity=post_identity,
            environment_identity=environment_identity,
            error_code=self._error_code(exc),
            message=str(exc) or type(exc).__name__,
            details=details,
            failure_domain=str(domain) if isinstance(domain, str) else None,
            changed_paths=changed_paths,
            history=(),
            changed_path_hashes=self._changed_path_hashes(plan.workspace_id, changed_paths),
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

        def attach_failure(record: WorkspaceRecord) -> None:
            raw_ids = record.metadata.get("failure_evidence_ids", ())
            identifiers = (
                [str(item) for item in raw_ids] if isinstance(raw_ids, (list, tuple)) else []
            )
            identifiers = [item for item in identifiers if item != stored.failure_id]
            identifiers.append(stored.failure_id)
            record.metadata["failure_evidence_ids"] = identifiers[-20:]
            record.metadata["last_failure_evidence_id"] = stored.failure_id

        self.ctx.store.update(workspace_id, attach_failure)
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
