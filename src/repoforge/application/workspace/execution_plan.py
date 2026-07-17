"""Create, validate, and accept immutable workspace execution plans."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass

from ...domain.assessment import WorkspaceAssessment
from ...domain.diagnostics import DiagnosticMutability
from ...domain.errors import ErrorCode, RepoForgeError
from ...domain.execution_plan import (
    ExecutionPlan,
    ExecutionPlanBinding,
    ExecutionPlanState,
    PlanStage,
    PlanStageBoundary,
    PlanStageKind,
    PlanStageMutability,
    StageFailurePolicy,
    create_execution_plan,
    validate_plan_current,
)
from ...domain.risk import VerificationRecommendation, WorkspaceRiskAssessment
from ...ports.execution_plan_store import (
    ExecutionPlanAcceptanceStore,
    ExecutionPlanStore,
)
from ..context import ApplicationContext
from .assessment import WorkspaceAssessmentCommand, WorkspaceAssessmentReader


@dataclass(frozen=True, slots=True)
class CreateExecutionPlanCommand:
    workspace_id: str
    task_id: str | None = None
    expires_at: str | None = None


@dataclass(frozen=True, slots=True)
class AcceptExecutionPlanCommand:
    workspace_id: str
    plan_id: str
    task_id: str | None = None


@dataclass(frozen=True, slots=True)
class ExecutionPlanResult:
    plan_id: str
    plan_hash: str
    workspace_id: str
    task_id: str | None
    binding: dict[str, object]
    ordered_stages: tuple[dict[str, object], ...]
    final_profile: str
    stage_definition_hash: str
    created_at: str
    expires_at: str | None
    accepted: bool
    acceptance_id: str | None = None


def _stable_hash(value: WorkspaceRiskAssessment | VerificationRecommendation) -> str:
    payload = asdict(value)
    if isinstance(payload, dict):
        payload = {key: item for key, item in payload.items() if key != "assessment_snapshot_id"}
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def _stage_id(order: int, kind: PlanStageKind) -> str:
    return f"stage-{order:02d}-{kind.value}"


class ExecutionPlanService:
    def __init__(self, ctx: ApplicationContext) -> None:
        self.ctx = ctx
        self._assessment = WorkspaceAssessmentReader(ctx)

    def _stores(self) -> tuple[ExecutionPlanStore, ExecutionPlanAcceptanceStore]:
        if self.ctx.execution_plans is None or self.ctx.execution_plan_acceptances is None:
            raise RepoForgeError(
                "Execution plan stores are unavailable", code=ErrorCode.CONFIG_INVALID
            )
        return self.ctx.execution_plans, self.ctx.execution_plan_acceptances

    def _compile_stages(self, assessment: WorkspaceAssessment) -> tuple[PlanStage, ...]:
        recommendation = assessment.verification_recommendation
        if recommendation is None:
            raise RepoForgeError(
                "Workspace assessment has no verification recommendation",
                code=ErrorCode.STATE_INVALID,
            )
        _, repo, _ = self.ctx.workspace(assessment.snapshot.workspace_id)
        stages: list[PlanStage] = []
        for index, recommended in enumerate(recommendation.ordered_stages, start=1):
            dependencies = (stages[-1].stage_id,) if stages else ()
            if recommended.kind == "diagnostic" and recommended.diagnostic is not None:
                diagnostic = repo.diagnostics.get(recommended.diagnostic)
                if diagnostic is None:
                    raise RepoForgeError(
                        f"Recommended diagnostic no longer exists: {recommended.diagnostic}",
                        code=ErrorCode.STATE_INVALID,
                    )
                stages.append(
                    PlanStage(
                        stage_id=_stage_id(index, PlanStageKind.DIAGNOSTIC),
                        kind=PlanStageKind.DIAGNOSTIC,
                        target=diagnostic.diagnostic_id,
                        selector=recommended.selector,
                        dependencies=dependencies,
                        boundary=PlanStageBoundary.ITERATION,
                        working_directory=diagnostic.working_directory,
                        timeout_seconds=diagnostic.timeout_seconds,
                        mutability=(
                            PlanStageMutability.READ_ONLY
                            if diagnostic.mutability is DiagnosticMutability.READ_ONLY
                            else PlanStageMutability.WORKSPACE_WRITE
                        ),
                        network_policy=diagnostic.network_policy.value,
                        failure_policy=StageFailurePolicy.REQUIRED,
                        artifact_paths=diagnostic.artifact_paths,
                    )
                )
                continue
            if recommended.kind != "profile" or recommended.profile is None:
                raise RepoForgeError(
                    "Verification recommendation contains an unsupported stage",
                    code=ErrorCode.STATE_INVALID,
                )
            profile = repo.profiles.get(recommended.profile)
            if profile is None:
                raise RepoForgeError(
                    f"Recommended profile no longer exists: {recommended.profile}",
                    code=ErrorCode.STATE_INVALID,
                )
            final = profile.name == recommendation.final_profile
            stages.append(
                PlanStage(
                    stage_id=_stage_id(index, PlanStageKind.PROFILE),
                    kind=PlanStageKind.PROFILE,
                    target=profile.name,
                    selector=None,
                    dependencies=dependencies,
                    boundary=PlanStageBoundary.FINAL if final else PlanStageBoundary.ITERATION,
                    working_directory=profile.working_directory,
                    timeout_seconds=profile.timeout_seconds
                    or (
                        self.ctx.config.server.verification_timeout_seconds
                        if profile.verification
                        else self.ctx.config.server.default_command_timeout_seconds
                    ),
                    mutability=PlanStageMutability.WORKSPACE_WRITE,
                    network_policy="local_only",
                    failure_policy=StageFailurePolicy.REQUIRED,
                    artifact_paths=(),
                )
            )
        return tuple(stages)

    def _build(
        self,
        assessment: WorkspaceAssessment,
        *,
        task_id: str | None,
        created_at: str,
        expires_at: str | None,
    ) -> ExecutionPlan:
        risk = assessment.risk
        recommendation = assessment.verification_recommendation
        if risk is None or recommendation is None:
            raise RepoForgeError(
                "Workspace assessment lacks risk and verification evidence",
                code=ErrorCode.STATE_INVALID,
            )
        return create_execution_plan(
            task_id=task_id,
            workspace_id=assessment.snapshot.workspace_id,
            binding=ExecutionPlanBinding(
                head_sha=assessment.snapshot.head_sha,
                workspace_fingerprint=assessment.snapshot.workspace_fingerprint,
                config_generation=assessment.snapshot.config_generation,
                policy_hash=assessment.snapshot.policy_hash,
                assessment_snapshot_id=assessment.snapshot.snapshot_id,
                evidence_snapshot_ids=(assessment.snapshot.snapshot_id,),
                risk_assessment_hash=_stable_hash(risk),
                recommendation_hash=_stable_hash(recommendation),
            ),
            ordered_stages=self._compile_stages(assessment),
            final_profile=recommendation.final_profile,
            created_at=created_at,
            expires_at=expires_at,
        )

    @staticmethod
    def _result(
        plan: ExecutionPlan,
        *,
        accepted: bool,
        acceptance_id: str | None = None,
    ) -> ExecutionPlanResult:
        return ExecutionPlanResult(
            plan_id=plan.plan_id,
            plan_hash=plan.plan_hash,
            workspace_id=plan.workspace_id,
            task_id=plan.task_id,
            binding=plan.binding.payload(),
            ordered_stages=tuple(stage.definition_payload() for stage in plan.ordered_stages),
            final_profile=plan.final_profile,
            stage_definition_hash=plan.stage_definition_hash,
            created_at=plan.created_at,
            expires_at=plan.expires_at,
            accepted=accepted,
            acceptance_id=acceptance_id,
        )

    def create(self, command: CreateExecutionPlanCommand) -> ExecutionPlanResult:
        plans, acceptances = self._stores()

        def operation() -> ExecutionPlanResult:
            assessment = self._assessment.execute(WorkspaceAssessmentCommand(command.workspace_id))
            plan = self._build(
                assessment,
                task_id=command.task_id,
                created_at=self.ctx.clock.now_iso(),
                expires_at=command.expires_at,
            )
            stored = plans.create(plan).value
            accepted = acceptances.read_for_plan(stored.plan_id)
            return self._result(
                stored,
                accepted=accepted is not None,
                acceptance_id=accepted.value.acceptance_id if accepted is not None else None,
            )

        return self.ctx.audited(
            "workspace_create_execution_plan",
            {"workspace_id": command.workspace_id},
            operation,
            mutating=False,
        )

    def current_state(self, plan: ExecutionPlan) -> ExecutionPlanState:
        assessment = self._assessment.execute(WorkspaceAssessmentCommand(plan.workspace_id))
        fresh = self._build(
            assessment,
            task_id=plan.task_id,
            created_at=plan.created_at,
            expires_at=plan.expires_at,
        )
        return ExecutionPlanState(
            head_sha=assessment.snapshot.head_sha,
            workspace_fingerprint=assessment.snapshot.workspace_fingerprint,
            config_generation=assessment.snapshot.config_generation,
            policy_hash=assessment.snapshot.policy_hash,
            risk_assessment_hash=fresh.binding.risk_assessment_hash,
            recommendation_hash=fresh.binding.recommendation_hash,
            stage_definition_hash=fresh.stage_definition_hash,
            now=self.ctx.clock.now_iso(),
        )

    def _raise_stale(self, plan: ExecutionPlan, reasons: tuple[str, ...]) -> None:
        raise RepoForgeError(
            "Execution plan no longer matches the current workspace",
            code=ErrorCode.STATE_STALE,
            retryable=True,
            safe_next_action="Create and accept a fresh execution plan from the current assessment.",
            details={"plan_id": plan.plan_id, "stale_reasons": list(reasons)},
        )

    def require_current(self, plan: ExecutionPlan) -> None:
        reasons = validate_plan_current(plan, self.current_state(plan))
        if reasons:
            self._raise_stale(plan, reasons)

    def _assert_local_binding_locked(self, plan: ExecutionPlan) -> None:
        _, repo, path = self.ctx.workspace(plan.workspace_id)
        actual = {
            "head_sha": self.ctx.git.head_sha(path).lower(),
            "workspace_fingerprint": self.ctx.git.fingerprint(path),
            "config_generation": self._assessment._config_generation(),
            "policy_hash": self._assessment._policy_hash(repo),
        }
        expected = {
            "head_sha": plan.binding.head_sha,
            "workspace_fingerprint": plan.binding.workspace_fingerprint,
            "config_generation": plan.binding.config_generation,
            "policy_hash": plan.binding.policy_hash,
        }
        reasons = tuple(name for name in expected if actual[name] != expected[name])
        if reasons:
            self._raise_stale(plan, reasons)

    def read_accepted(self, workspace_id: str, plan_id: str) -> ExecutionPlan:
        plans, acceptances = self._stores()
        envelope = plans.read(plan_id)
        if envelope is None:
            raise RepoForgeError(
                f"Execution plan not found: {plan_id}",
                code=ErrorCode.STATE_NOT_FOUND,
            )
        plan = envelope.value
        if plan.workspace_id != workspace_id:
            raise RepoForgeError(
                "Execution plan belongs to a different workspace",
                code=ErrorCode.STATE_INVALID,
            )
        acceptance = acceptances.read_for_plan(plan_id)
        if acceptance is None or acceptance.value.plan_hash != plan.plan_hash:
            raise RepoForgeError(
                "Execution plan has not been accepted for execution",
                code=ErrorCode.APPROVAL_REQUIRED,
                safe_next_action="Accept the exact current plan before executing it.",
            )
        return plan

    def accept(self, command: AcceptExecutionPlanCommand) -> ExecutionPlanResult:
        plans, acceptances = self._stores()
        envelope = plans.read(command.plan_id)
        if envelope is None:
            raise RepoForgeError(
                f"Execution plan not found: {command.plan_id}",
                code=ErrorCode.STATE_NOT_FOUND,
            )
        plan = envelope.value
        if plan.workspace_id != command.workspace_id:
            raise RepoForgeError(
                "Execution plan belongs to a different workspace",
                code=ErrorCode.STATE_INVALID,
            )

        def operation() -> ExecutionPlanResult:
            # Collect and validate the full assessment before taking the workspace
            # mutation lock; assessment components may take that same lock internally.
            self.require_current(plan)
            with self.ctx.locks.lock(command.workspace_id):
                self._assert_local_binding_locked(plan)
                accepted = acceptances.accept(
                    plan,
                    acceptance_id=f"accept-{self.ctx.ids.new_hex(24)}",
                    task_id=command.task_id,
                    accepted_at=self.ctx.clock.now_iso(),
                )
                record = self.ctx.store.load(command.workspace_id)
                record.metadata["accepted_plan_id"] = plan.plan_id
                record.metadata["execution_plan_id"] = plan.plan_id
                record.metadata["plan_receipt"] = plan.plan_hash
                self.ctx.store.save(record)
                return self._result(
                    plan,
                    accepted=True,
                    acceptance_id=accepted.value.acceptance_id,
                )

        return self.ctx.audited(
            "workspace_accept_execution_plan",
            {"workspace_id": command.workspace_id, "plan_id": command.plan_id},
            operation,
            mutating=True,
        )
