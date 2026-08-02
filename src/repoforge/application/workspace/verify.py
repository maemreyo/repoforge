"""Unified planning and execution orchestration for workspace verification."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any, Literal

from ...domain.diagnostics import DiagnosticExpectation, DiagnosticFailureClass
from ...domain.errors import (
    CommandError,
    ConfigError,
    ErrorCode,
    RepoForgeError,
    SecurityError,
    WorkspaceError,
)
from ...domain.excerpts import bound_command_excerpt
from ...domain.filesystem_transaction import CreateFile, TransactionPlan, WriteFile
from ...domain.operation_task import (
    TERMINAL_OPERATION_STATES,
    OperationRetryability,
    OperationState,
    OperationTask,
)
from ...domain.operation_work import OperationWorkRequest
from ...domain.policy import assert_path_allowed, resolve_workspace_path
from ...domain.redaction import sanitize_persisted_data
from ...domain.verification import VerificationIntent, select_verification_profile
from ..audit_context import current_audit_attribution
from ..context import ApplicationContext
from ..dto import to_data
from ..file_transactions import open_file_transaction
from ..fingerprint_cache import prime_fingerprint
from ..operations.manager import OperationManager
from ..operations.work_admission import DurableWorkAdmission
from .assessment import WorkspaceAssessmentCommand, WorkspaceAssessmentReader
from .diagnostic_selector import SelectorInput
from .run_adhoc import (
    WorkspaceAdhocRunner,
    WorkspaceRunAdhocBackgroundResult,
    WorkspaceRunAdhocResult,
)
from .run_diagnostic import (
    WorkspaceDiagnosticRunner,
    WorkspaceRunDiagnosticBackgroundResult,
    WorkspaceRunDiagnosticResult,
)
from .run_profile import (
    WorkspaceProfileRunner,
    WorkspaceRunProfileBackgroundResult,
    WorkspaceRunProfileResult,
)
from .snapshot import WorkspaceSnapshotReader

VerifyMode = Literal["plan", "auto", "diagnostic", "profile", "adhoc"]
VerifyRerun = Literal["failed"]
_HIGH_CONFIDENCE = 95
_MAX_ARTIFACT_BYTES = 120_000
_FOREGROUND_WAIT_SECONDS = 25.0
_FOREGROUND_POLL_SECONDS = 0.1


@dataclass(frozen=True, slots=True)
class WorkspaceVerifyCommand:
    workspace_id: str
    mode: VerifyMode = "auto"
    diagnostic_id: str | None = None
    selector: SelectorInput = None
    selector2: SelectorInput = None
    profile_name: str | None = None
    argv: tuple[str, ...] | None = None
    working_directory: str | None = None
    expected_fingerprint: str | None = None
    expected_head_sha: str | None = None
    mutability: str = "read_only"
    background: bool = False
    intent: VerificationIntent | str | None = None
    expectation: DiagnosticExpectation | str | None = None
    expected_failure_class: DiagnosticFailureClass | str | None = None
    force_rerun: bool = False
    rerun: VerifyRerun | None = None
    impact_paths: tuple[str, ...] = ()
    artifact_output_path: str | None = None
    stdin_text: str | None = None


@dataclass(frozen=True, slots=True)
class WorkspaceVerifyResult:
    summary: str
    workspace_id: str
    requested_mode: str
    selected_mode: str
    routing_reason: str
    impact_evidence: dict[str, object] | None
    assessment: dict[str, object] | None
    recommendations: list[dict[str, object]]
    staleness_warning: str | None
    operation: dict[str, object] | None
    commands: list[dict[str, object]]
    steps: list[dict[str, object]]
    failed_step: dict[str, object] | None
    failure_domain: str | None
    business_tests_ran: bool
    valid_tdd_red_evidence: bool
    failure_reused: bool
    artifact_paths: list[str]
    outcome: str
    satisfies_commit_gate: bool
    head_sha: str
    workspace_fingerprint: str
    execution_evidence: dict[str, object] | None = None
    # Set only for mode=adhoc. Carries the two facts the ad-hoc runner computes that no
    # other section can express: whether RepoForge inspected the command's content at
    # all, and whether a command declared read-only actually moved the tree. Both are
    # load-bearing for an opaque runner (a shell, `uv`, `python3`), where the git argv
    # guards never ran and the fingerprint is the only behavioural check left.
    adhoc_evidence: dict[str, object] | None = None
    # What the caller should do when the call returns before the work is done -- the
    # exact `operation` wait for a background run. Set only then; a finished verify
    # answers with its outcome.
    next_action: str | None = None
    failed_selectors: list[str] = field(default_factory=list)
    output_artifact_reference: str | None = None
    failure_provider: str | None = None
    selector_coverage: str = "not_applicable"
    selectors_unavailable_reason: str | None = None
    failure_locations: list[dict[str, object]] = field(default_factory=list)
    output_artifact_status: str = "not_applicable"
    failure_expectation: str | None = None
    failure_chain_id: str | None = None
    rerun_of_selectors: list[str] = field(default_factory=list)


def _string_list(value: object) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [item for item in value if isinstance(item, str)]


def _assessment_projection(assessment: Any) -> tuple[dict[str, object], dict[str, object] | None]:
    risk = assessment.risk
    recommendation = assessment.verification_recommendation
    if risk is None or recommendation is None:
        raise WorkspaceError("Workspace assessment did not produce verification recommendations")
    changed_paths = _string_list(assessment.changed_paths.value.get("paths"))
    base = assessment.base_freshness.value
    refresh_required = bool(base.get("refresh_required", False))
    behind_base = base.get("behind_base", 0)
    if not isinstance(behind_base, int) or isinstance(behind_base, bool) or behind_base < 0:
        behind_base = 0
    intelligence = assessment.code_intelligence.value
    provider_id = intelligence.get("provider_id")
    confidence = intelligence.get("confidence")
    impact_evidence: dict[str, object] | None = None
    if isinstance(provider_id, str) and provider_id:
        confidence_value = confidence.get("value", 0) if isinstance(confidence, dict) else 0
        if not isinstance(confidence_value, int) or isinstance(confidence_value, bool):
            confidence_value = 0
        impact_evidence = {
            "provider": provider_id,
            "confidence": max(0.0, min(1.0, confidence_value / 100)),
            "coverage": _string_list(intelligence.get("analyzed_paths"))[:100],
            "limitations": _string_list(intelligence.get("limitations"))[:100],
        }
    projection = {
        "snapshot_id": assessment.snapshot.snapshot_id,
        "current": assessment.current,
        "changed_paths": changed_paths,
        "risk_score": risk.score,
        "risk_level": risk.level.value,
        "uncertainties": list(assessment.uncertainties),
        "refresh_required": refresh_required,
        "behind_base": behind_base,
        "base_freshness": {
            "status": assessment.base_freshness.status.value,
            "coverage": assessment.base_freshness.coverage.value,
            "value": dict(base) if base else None,
            "error_code": assessment.base_freshness.error_code,
            "safe_fallback": assessment.base_freshness.safe_fallback,
        },
        "provider": impact_evidence,
        "final_profile": recommendation.final_profile,
        "manual_review_required": recommendation.manual_review_required,
        "evidence_coverage": [
            {"key": key, "value": value}
            for key, value in sorted(assessment.evidence_coverage.items())
        ],
    }
    return projection, impact_evidence


def _recommendations(assessment: Any) -> list[dict[str, object]]:
    recommendation = assessment.verification_recommendation
    if recommendation is None:
        return []
    return [
        {
            "order": stage.order,
            "kind": stage.kind,
            "reason": stage.reason,
            "diagnostic_id": stage.diagnostic,
            "profile_name": stage.profile,
            "selector": stage.selector,
        }
        for stage in recommendation.ordered_stages
    ]


def _staleness_warning(assessment: Any) -> str | None:
    value = assessment.base_freshness.value
    if not bool(value.get("refresh_required", False)):
        return None
    warning = value.get("warning")
    if not isinstance(warning, str):
        warning = value.get("preflight_warning")
    recommended = value.get("recommended_action")
    invalidated = value.get("expected_evidence_invalidation")
    if isinstance(warning, str):
        suffix = ""
        if isinstance(recommended, str):
            suffix += f" Recommended action: {recommended}."
        if isinstance(invalidated, list) and invalidated:
            suffix += " Expected invalidation: " + ", ".join(map(str, invalidated)) + "."
        return warning + suffix
    behind = value.get("behind_base", 0)
    if isinstance(behind, int) and not isinstance(behind, bool) and behind > 0:
        return (
            f"Base is {behind} commit(s) behind; full-run results will be invalidated by refresh. "
            "Consider refreshing first, or continue to isolate pre-refresh failures."
        )
    return (
        "Base freshness indicates refresh is required; full-run results may be invalidated by "
        "refresh. Consider refreshing first, or continue to isolate pre-refresh failures."
    )


def _auto_target(assessment: Any) -> tuple[str, list[str], str] | None:
    code_intelligence = assessment.code_intelligence
    if (
        code_intelligence.status.value != "current"
        or code_intelligence.coverage.value != "complete"
    ):
        return None
    candidates = code_intelligence.value.get("affected_tests")
    if not isinstance(candidates, list):
        return None
    grouped: dict[str, set[str]] = {}
    reasons: list[str] = []
    for raw in candidates:
        if not isinstance(raw, dict):
            continue
        diagnostic = raw.get("diagnostic_id")
        selector = raw.get("selector")
        confidence = raw.get("confidence")
        reason = raw.get("reason")
        if (
            isinstance(diagnostic, str)
            and diagnostic
            and isinstance(selector, str)
            and selector
            and isinstance(confidence, int)
            and not isinstance(confidence, bool)
            and confidence >= _HIGH_CONFIDENCE
        ):
            grouped.setdefault(diagnostic, set()).add(selector)
            if isinstance(reason, str) and reason:
                reasons.append(reason)
    if len(grouped) != 1:
        return None
    diagnostic, selectors = next(iter(grouped.items()))
    if not selectors:
        return None
    routing_reason = (
        f"Current code-intelligence evidence routed {len(selectors)} exact affected-test "
        f"selector(s) at or above {_HIGH_CONFIDENCE}% confidence."
    )
    if reasons:
        routing_reason += f" {reasons[0]}"
    return diagnostic, sorted(selectors), routing_reason


def _selector_tuple(raw: SelectorInput) -> tuple[str, ...] | None:
    if raw is None:
        return None
    if isinstance(raw, str):
        return (raw,)
    return tuple(raw)


def _enum_value(raw: object) -> str | None:
    if raw is None:
        return None
    value = getattr(raw, "value", raw)
    return str(value)


def _command_evidence(raw: dict[str, object]) -> dict[str, object]:
    argv = _string_list(raw.get("argv"))
    returncode = raw.get("returncode", 0)
    if not isinstance(returncode, int) or isinstance(returncode, bool):
        returncode = 0
    duration = raw.get("duration_ms", 0.0)
    if not isinstance(duration, (int, float)) or isinstance(duration, bool) or duration < 0:
        duration = 0.0
    stdout = raw.get("stdout", "")
    stderr = raw.get("stderr", "")
    excerpt = "\n".join(item for item in (stdout, stderr) if isinstance(item, str) and item)
    return {
        "argv": argv,
        "returncode": returncode,
        "duration_ms": float(duration),
        "output_excerpt": bound_command_excerpt(excerpt, 12_000),
    }


def _adhoc_evidence(result: WorkspaceRunAdhocResult) -> dict[str, object]:
    """Project the ad-hoc runner's own policy facts onto the verify surface.

    ``command_class`` is ``None`` exactly when RepoForge did not inspect the command's
    content -- every runner other than ``git``, a shell included. Publishing that as
    ``content_inspected`` keeps the caller from reading the git argv guards as though
    they had applied to a ``bash -c`` line they never saw.
    """
    return {
        "mutability": result.mutability,
        "command_class": result.command_class,
        "content_inspected": result.command_class is not None,
        "fingerprint_changed": result.fingerprint_changed,
        "read_only_violation": result.read_only_violation,
        "changed_paths": list(result.changed_paths),
        "changed_paths_truncated": result.changed_paths_truncated,
        "network_policy": result.network_policy,
        "verification_invalidated": result.verification_invalidated,
    }


def _profile_steps(result: WorkspaceRunProfileResult) -> list[dict[str, object]]:
    timing: dict[int, tuple[float | None, float | None]] = {}
    for command in result.commands:
        index = command.get("stage_index")
        if not isinstance(index, int) or isinstance(index, bool):
            continue
        duration = command.get("duration_ms")
        cumulative = command.get("cumulative_duration_ms")
        timing[index] = (
            float(duration) if isinstance(duration, (int, float)) else None,
            float(cumulative) if isinstance(cumulative, (int, float)) else None,
        )
    steps: list[dict[str, object]] = []
    for status, raw_steps in (
        ("completed", result.completed_steps),
        ("not_run", result.not_run_steps),
    ):
        for raw in raw_steps:
            step_index = len(steps)
            duration, cumulative = timing.get(step_index, (None, None))
            steps.append(
                {
                    "id": str(raw.get("id", f"step-{step_index + 1}")),
                    "kind": str(raw.get("kind", "unknown")),
                    "status": status,
                    "duration_ms": duration,
                    "cumulative_duration_ms": cumulative,
                    "failure_domain": None,
                }
            )
    return steps


class WorkspaceVerifier:
    def __init__(
        self,
        ctx: ApplicationContext,
        *,
        assessment: WorkspaceAssessmentReader,
        snapshot: WorkspaceSnapshotReader,
        profile: WorkspaceProfileRunner,
        diagnostic: WorkspaceDiagnosticRunner,
        adhoc: WorkspaceAdhocRunner,
        admission: DurableWorkAdmission,
        operations: OperationManager,
    ) -> None:
        self.ctx = ctx
        self._assessment = assessment
        self._snapshot = snapshot
        self._profile = profile
        self._diagnostic = diagnostic
        self._adhoc = adhoc
        self._admission = admission
        self._operations = operations

    def _refuse_rerun_too_soon(self, workspace_id: str, profile_name: str) -> None:
        """Bound how often a model may repeat an expensive profile on one workspace.

        Receipt reuse already covers an unchanged tree, but `force_rerun` exists so an
        external change can be re-verified WITHOUT the tree changing -- and that same flag
        turns a 30-minute gate into something repeatable at will. This is the floor under it,
        expressed in the reviewed configuration. Operator and CI runs are never bounded:
        they have weighed the cost.
        """
        if current_audit_attribution().origin != "model":
            return
        _record, repo, _path = self.ctx.workspace(workspace_id)
        profile = repo.profiles.get(profile_name)
        if profile is None or profile.min_interval_seconds <= 0:
            return
        try:
            cutoff = (
                datetime.fromisoformat(self.ctx.clock.now_iso())
                - timedelta(seconds=profile.min_interval_seconds)
            ).isoformat()
        except ValueError:
            # An unreadable clock is not a reason to refuse expensive-but-wanted work.
            return
        for task in self._operations.list_records(max_records=200).records:
            if task.workspace_id != workspace_id or task.kind != "workspace_run_profile":
                continue
            if task.state not in TERMINAL_OPERATION_STATES or task.updated_at <= cutoff:
                continue
            raise CommandError(
                f"PROFILE_RERUN_TOO_SOON: profile {profile_name!r} ran on this workspace "
                f"less than {profile.min_interval_seconds}s ago "
                f"(operation {task.operation_id}).",
                code=ErrorCode.PROFILE_RERUN_TOO_SOON,
                retryable=False,
                safe_next_action=(
                    f"Read the result of {task.operation_id} instead of re-running it, or "
                    "ask the operator to run it if an external condition changed."
                ),
            )

    def _refuse_reserved_profile(self, workspace_id: str, profile_name: str) -> None:
        """Refuse a model-initiated run of a profile the reviewed configuration reserves.

        An authoritative gate can cost half an hour of machine time. A model cannot weigh
        that, and on this installation one started the 30-minute `full` gate to sanity-check
        an unrelated runtime fix -- a reasonable-looking call, and the wrong one. Putting the
        judgement in the reviewed configuration makes it the operator's, enforceable, and
        auditable; an instruction to the agent would have been none of those.
        """
        if current_audit_attribution().origin != "model":
            return
        _record, repo, _path = self.ctx.workspace(workspace_id)
        profile = repo.profiles.get(profile_name)
        if profile is None or profile.model_invocable:
            return
        raise CommandError(
            f"PROFILE_NOT_MODEL_INVOCABLE: profile {profile_name!r} is reserved for the "
            "operator and CI by the reviewed configuration.",
            code=ErrorCode.PROFILE_NOT_MODEL_INVOCABLE,
            retryable=False,
            safe_next_action=(
                "Use a profile the configuration exposes to the model, or ask the operator "
                f"to run {profile_name!r} out of band."
            ),
        )

    def _wait_for_operation(
        self,
        operation_id: str,
    ) -> tuple[OperationTask, dict[str, Any] | None]:
        deadline = time.monotonic() + _FOREGROUND_WAIT_SECONDS
        task = self._operations.status(operation_id)
        while task.state not in TERMINAL_OPERATION_STATES and time.monotonic() < deadline:
            time.sleep(_FOREGROUND_POLL_SECONDS)
            task = self._operations.status(operation_id)
        result = None
        if task.state is OperationState.SUCCEEDED and self.ctx.operation_result_store is not None:
            result = self.ctx.operation_result_store.read(operation_id)
        self._raise_terminal_failure(task)
        return task, result

    @staticmethod
    def _raise_terminal_failure(task: OperationTask) -> None:
        """Surface a durable execution failure to the caller that waited for it.

        Making execution durable moved the command off the request thread; it did
        not turn a refusal into a verdict. A caller that waited for this operation
        would otherwise receive `outcome="failed"` with the exact reason -- the
        typed code and message -- reachable only through a second call, so the
        terminal failure is re-raised here with its evidence intact.
        """
        if task.state not in {
            OperationState.FAILED,
            OperationState.ORPHANED,
            OperationState.CANCELLED,
        }:
            return
        try:
            code = ErrorCode(str(task.error_code))
        except ValueError:
            code = (
                ErrorCode.COMMAND_FAILED
                if task.state is OperationState.CANCELLED
                else ErrorCode.INTERNAL_ERROR
            )
        message = task.error_message or (
            f"Durable verification operation {task.operation_id} {task.state.value}"
        )
        raise RepoForgeError(
            message,
            code=code,
            retryable=task.retryability is OperationRetryability.AUTOMATIC,
            safe_next_action=(f"Read operation {task.operation_id} for the full durable evidence."),
            details={
                "operation_id": task.operation_id,
                "operation_state": task.state.value,
                "operation_phase": task.phase,
                "attempt": task.attempt,
            },
        )

    @staticmethod
    def _operation_projection(task: OperationTask) -> dict[str, object]:
        return {
            "operation_id": task.operation_id,
            "kind": task.kind,
            "state": task.state.value,
            "phase": task.phase,
            "progress_current": task.progress_current,
            "progress_total": task.progress_total,
            "cancellation_reason": (
                "cancelled" if task.state is OperationState.CANCELLED else None
            ),
            "poll_after_seconds": (None if task.state in TERMINAL_OPERATION_STATES else 1.0),
        }

    def _project_operation(
        self,
        result: WorkspaceVerifyResult,
        task: OperationTask,
    ) -> WorkspaceVerifyResult:
        terminal = task.state in TERMINAL_OPERATION_STATES
        return replace(
            result,
            summary=(
                f"Durable verification operation {task.operation_id} {task.state.value}"
                if terminal
                else "Durable verification is queued or running"
            ),
            operation=self._operation_projection(task),
            outcome=task.state.value if terminal else "running",
            satisfies_commit_gate=False,
        )

    def execute(self, command: WorkspaceVerifyCommand) -> WorkspaceVerifyResult:
        audit_details: dict[str, object] = {
            "workspace_id": command.workspace_id,
            "requested_mode": command.mode,
            "background": command.background,
            "force_rerun": command.force_rerun,
            "rerun": command.rerun,
            "impact_path_count": len(command.impact_paths),
            "artifact_output_requested": command.artifact_output_path is not None,
        }
        return self.ctx.audited(
            "workspace_verify",
            audit_details,
            lambda: self._execute(command),
            mutating=command.mode != "plan",
        )

    def _execute(self, command: WorkspaceVerifyCommand) -> WorkspaceVerifyResult:
        if command.mode not in {"plan", "auto", "diagnostic", "profile", "adhoc"}:
            raise ConfigError(f"Unknown workspace_verify mode: {command.mode}")
        if command.rerun is not None:
            if command.rerun != "failed":
                raise ConfigError(f"Unknown workspace_verify rerun mode: {command.rerun}")
            if command.mode != "diagnostic" or command.diagnostic_id is None:
                raise ConfigError("rerun=failed requires diagnostic mode and diagnostic_id")
            if command.selector is not None or command.selector2 is not None:
                raise ConfigError("rerun=failed restores the exact recorded selectors")
        if command.mode == "plan" and (command.background or command.artifact_output_path):
            raise ConfigError(
                "workspace_verify plan mode is read-only and cannot run in background or write artifacts"
            )
        if command.background and command.artifact_output_path is not None:
            raise ConfigError(
                "Background workspace_verify cannot write a synchronous artifact output"
            )
        assessment: Any | None = None
        assessment_projection: dict[str, object] | None = None
        impact_evidence: dict[str, object] | None = None
        recommendations: list[dict[str, object]] = []
        warning: str | None = None
        final_profile = ""
        if command.mode in {"plan", "auto"}:
            assessment = self._assessment.execute(
                WorkspaceAssessmentCommand(command.workspace_id, command.impact_paths)
            )
            assessment_projection, impact_evidence = _assessment_projection(assessment)
            recommendations = _recommendations(assessment)
            warning = _staleness_warning(assessment)
            recommendation = assessment.verification_recommendation
            if recommendation is None:
                raise WorkspaceError("Workspace assessment did not produce a final profile")
            final_profile = recommendation.final_profile
            head_sha = assessment.snapshot.head_sha
            workspace_fingerprint = assessment.snapshot.workspace_fingerprint
        else:
            snapshot = self._snapshot.capture(command.workspace_id, command.impact_paths)
            head_sha = snapshot.head_sha
            workspace_fingerprint = snapshot.workspace_fingerprint
            if command.mode == "profile":
                _record, repo, _path = self.ctx.workspace(command.workspace_id)
                selected_profile, _used_default = select_verification_profile(
                    repo, command.profile_name
                )
                final_profile = selected_profile.name

        if (
            command.expected_fingerprint is not None
            and command.expected_fingerprint != workspace_fingerprint
        ):
            raise WorkspaceError(
                "Workspace changed since the requested verification snapshot was reviewed"
            )
        if command.expected_head_sha is not None and command.expected_head_sha != head_sha:
            raise WorkspaceError(
                "STALE_STATE: workspace HEAD changed since the requested verification snapshot "
                "was reviewed",
                code=ErrorCode.STALE_STATE,
                retryable=True,
                details={
                    "expected_head_sha": command.expected_head_sha,
                    "actual_head_sha": head_sha,
                },
            )

        if command.mode == "plan":
            return WorkspaceVerifyResult(
                summary="Planned workspace verification without running subprocesses",
                workspace_id=command.workspace_id,
                requested_mode="plan",
                selected_mode="plan",
                routing_reason=(
                    "Plan mode returns the current assessment and ordered recommendations only."
                ),
                impact_evidence=impact_evidence,
                assessment=assessment_projection,
                recommendations=recommendations,
                staleness_warning=warning,
                operation=None,
                commands=[],
                steps=[],
                failed_step=None,
                failure_domain=None,
                business_tests_ran=False,
                valid_tdd_red_evidence=False,
                failure_reused=False,
                artifact_paths=[],
                outcome="planned",
                satisfies_commit_gate=False,
                head_sha=head_sha,
                workspace_fingerprint=workspace_fingerprint,
            )

        selected_mode = command.mode
        routing_reason = f"Explicit {command.mode} mode was requested."
        fallback_full = False
        diagnostic_id = command.diagnostic_id
        selector: SelectorInput = command.selector
        profile_name = command.profile_name
        if command.mode == "auto":
            assert assessment is not None
            targeted = _auto_target(assessment)
            if targeted is not None:
                diagnostic_id, selector, routing_reason = targeted
                selected_mode = "diagnostic"
            else:
                selected_mode = "profile"
                profile_name = final_profile
                fallback_full = True
                status = assessment.code_intelligence.status.value
                routing_reason = (
                    f"Code-intelligence evidence is {status} or below {_HIGH_CONFIDENCE}% confidence; "
                    f"falling back to final profile {final_profile!r}."
                )

        if selected_mode == "diagnostic":
            if not diagnostic_id:
                raise ConfigError("diagnostic mode requires diagnostic_id")
            admitted = self._admission.admit(
                OperationWorkRequest.diagnostic(
                    workspace_id=command.workspace_id,
                    diagnostic_id=diagnostic_id,
                    selector=_selector_tuple(selector),
                    selector2=_selector_tuple(command.selector2),
                    intent=_enum_value(command.intent),
                    expectation=_enum_value(command.expectation),
                    expected_failure_class=_enum_value(command.expected_failure_class),
                    force_rerun=command.force_rerun,
                    rerun_failed=command.rerun == "failed",
                    expected_head_sha=head_sha,
                    expected_fingerprint=workspace_fingerprint,
                    config_generation=self.ctx.config_generation,
                ),
                operation_kind="workspace_run_diagnostic",
            )
            durable_task = admitted
            diagnostic_stored_result: dict[str, Any] | None = None
            if not command.background:
                durable_task, diagnostic_stored_result = self._wait_for_operation(
                    admitted.operation_id
                )
            if (
                durable_task.state is OperationState.SUCCEEDED
                and diagnostic_stored_result is not None
            ):
                diagnostic_result: (
                    WorkspaceRunDiagnosticResult | WorkspaceRunDiagnosticBackgroundResult
                ) = WorkspaceRunDiagnosticResult(**diagnostic_stored_result)
            else:
                diagnostic_result = WorkspaceRunDiagnosticBackgroundResult(
                    operation_id=admitted.operation_id,
                    phase=durable_task.phase,
                    safe_next_action=(
                        "Wait for operation "
                        f"{admitted.operation_id} with until='terminal' and "
                        "timeout_seconds=60, re-issuing the same call while it times out. "
                        "60 is the safe default: some clients block a tool call held much "
                        "longer, and a blocked call costs a whole turn while a re-issued "
                        "wait costs nothing. Do not spin on operation get -- progress mode "
                        "wakes you once per step and tells you nothing extra."
                    ),
                )
            result = self._from_diagnostic(
                command,
                diagnostic_result,
                routing_reason,
                assessment_projection,
                impact_evidence,
                recommendations,
                warning,
                head_sha=head_sha,
                workspace_fingerprint=workspace_fingerprint,
            )
            if isinstance(diagnostic_result, WorkspaceRunDiagnosticBackgroundResult):
                result = self._project_operation(result, durable_task)
        elif selected_mode == "profile":
            # Enforced HERE, on the model-facing side of the durable boundary. The runner
            # executes later in the worker process, whose attribution is background_worker,
            # so a guard placed there would never see the model that asked for the run.
            self._refuse_reserved_profile(command.workspace_id, profile_name or final_profile)
            self._refuse_rerun_too_soon(command.workspace_id, profile_name or final_profile)
            admitted = self._admission.admit(
                OperationWorkRequest.profile(
                    workspace_id=command.workspace_id,
                    profile_name=profile_name or final_profile,
                    expected_head_sha=head_sha,
                    expected_fingerprint=workspace_fingerprint,
                    config_generation=self.ctx.config_generation,
                ),
                operation_kind="workspace_run_profile",
            )
            durable_task = admitted
            profile_stored_result: dict[str, Any] | None = None
            if not command.background:
                durable_task, profile_stored_result = self._wait_for_operation(
                    admitted.operation_id
                )
            if durable_task.state is OperationState.SUCCEEDED and profile_stored_result is not None:
                profile_result: WorkspaceRunProfileResult | WorkspaceRunProfileBackgroundResult = (
                    WorkspaceRunProfileResult(**profile_stored_result)
                )
            else:
                profile_result = WorkspaceRunProfileBackgroundResult(
                    operation_id=admitted.operation_id,
                    phase=durable_task.phase,
                    safe_next_action=(
                        "Wait for operation "
                        f"{admitted.operation_id} with until='terminal' and "
                        "timeout_seconds=60, re-issuing the same call while it times out. "
                        "60 is the safe default: some clients block a tool call held much "
                        "longer, and a blocked call costs a whole turn while a re-issued "
                        "wait costs nothing. Do not spin on operation get -- progress mode "
                        "wakes you once per step and tells you nothing extra."
                    ),
                )
            result = self._from_profile(
                command,
                profile_result,
                routing_reason,
                assessment_projection,
                impact_evidence,
                recommendations,
                warning,
                head_sha=head_sha,
                workspace_fingerprint=workspace_fingerprint,
                fallback_full=fallback_full,
            )
            if isinstance(profile_result, WorkspaceRunProfileBackgroundResult):
                result = self._project_operation(result, durable_task)
        else:
            if command.argv is None:
                raise ConfigError("adhoc mode requires argv")
            admitted = self._admission.admit(
                OperationWorkRequest.adhoc(
                    workspace_id=command.workspace_id,
                    argv=command.argv,
                    working_directory=command.working_directory,
                    mutability=command.mutability,
                    expected_head_sha=head_sha,
                    expected_fingerprint=workspace_fingerprint,
                    config_generation=self.ctx.config_generation,
                    stdin_text=command.stdin_text,
                ),
                operation_kind="workspace_run_adhoc",
            )
            durable_task = admitted
            adhoc_stored_result: dict[str, Any] | None = None
            if not command.background:
                durable_task, adhoc_stored_result = self._wait_for_operation(admitted.operation_id)
            if durable_task.state is OperationState.SUCCEEDED and adhoc_stored_result is not None:
                adhoc_result: WorkspaceRunAdhocResult | WorkspaceRunAdhocBackgroundResult = (
                    WorkspaceRunAdhocResult(**adhoc_stored_result)
                )
            else:
                adhoc_result = WorkspaceRunAdhocBackgroundResult(
                    operation_id=admitted.operation_id,
                    phase=durable_task.phase,
                    safe_next_action=(
                        "Wait for operation "
                        f"{admitted.operation_id} with until='terminal' and "
                        "timeout_seconds=60, re-issuing the same call while it times out. "
                        "60 is the safe default: some clients block a tool call held much "
                        "longer, and a blocked call costs a whole turn while a re-issued "
                        "wait costs nothing. Do not spin on operation get -- progress mode "
                        "wakes you once per step and tells you nothing extra."
                    ),
                )
            result = self._from_adhoc(
                command,
                adhoc_result,
                routing_reason,
                assessment_projection,
                impact_evidence,
                recommendations,
                warning,
                head_sha=head_sha,
                workspace_fingerprint=workspace_fingerprint,
            )
            if isinstance(adhoc_result, WorkspaceRunAdhocBackgroundResult):
                result = self._project_operation(result, durable_task)

        if command.artifact_output_path is not None:
            if result.outcome == "running":
                return replace(result, output_artifact_status="source_unavailable")
            return self._persist_artifact(result, command.artifact_output_path)
        if result.outcome == "passed" and command.mode in {"auto", "profile"}:
            record = self.ctx.store.load(command.workspace_id)
            if "last_recreate_verify_selector" in record.metadata:
                record.metadata.pop("last_recreate_verify_selector", None)
                self.ctx.store.save(record)
        return result

    def _from_diagnostic(
        self,
        command: WorkspaceVerifyCommand,
        delegated: WorkspaceRunDiagnosticResult | WorkspaceRunDiagnosticBackgroundResult,
        reason: str,
        assessment: dict[str, object] | None,
        impact: dict[str, object] | None,
        recommendations: list[dict[str, object]],
        warning: str | None,
        *,
        head_sha: str,
        workspace_fingerprint: str,
    ) -> WorkspaceVerifyResult:
        if isinstance(delegated, WorkspaceRunDiagnosticBackgroundResult):
            return WorkspaceVerifyResult(
                summary="Workspace diagnostic is queued or running",
                next_action=delegated.safe_next_action,
                workspace_id=command.workspace_id,
                requested_mode=command.mode,
                selected_mode="diagnostic",
                routing_reason=reason,
                impact_evidence=impact,
                assessment=assessment,
                recommendations=recommendations,
                staleness_warning=warning,
                operation={
                    "operation_id": delegated.operation_id,
                    "kind": "workspace_run_diagnostic",
                    "state": "pending" if delegated.phase == "queued" else "running",
                    "phase": delegated.phase,
                    "progress_current": None,
                    "progress_total": None,
                    "cancellation_reason": None,
                    "poll_after_seconds": 1.0,
                },
                commands=[],
                steps=[],
                failed_step=None,
                failure_domain=None,
                business_tests_ran=False,
                valid_tdd_red_evidence=False,
                failure_reused=False,
                artifact_paths=[],
                outcome="running",
                satisfies_commit_gate=False,
                head_sha=head_sha,
                workspace_fingerprint=workspace_fingerprint,
            )
        command_raw = {
            "argv": delegated.argv,
            "returncode": delegated.returncode,
            "duration_ms": 0.0,
            "stdout": delegated.excerpt,
        }
        return WorkspaceVerifyResult(
            summary=f"Diagnostic {delegated.diagnostic_id} {delegated.outcome}",
            workspace_id=command.workspace_id,
            requested_mode=command.mode,
            selected_mode="diagnostic",
            routing_reason=reason,
            impact_evidence=impact,
            assessment=assessment,
            recommendations=recommendations,
            staleness_warning=warning,
            operation=None,
            commands=[_command_evidence(command_raw)],
            steps=[],
            failed_step=None,
            failure_domain=delegated.failure_class,
            business_tests_ran=delegated.business_tests_ran,
            valid_tdd_red_evidence=delegated.valid_tdd_red_evidence,
            failure_reused=delegated.failure_reused,
            artifact_paths=[],
            outcome=delegated.outcome,
            satisfies_commit_gate=delegated.satisfies_commit_gate,
            head_sha=delegated.head_sha,
            workspace_fingerprint=delegated.fingerprint_after,
            execution_evidence=delegated.execution_evidence,
            failed_selectors=delegated.failed_selectors,
            output_artifact_reference=delegated.output_artifact_reference,
            failure_provider=delegated.failure_provider,
            selector_coverage=delegated.selector_coverage,
            selectors_unavailable_reason=delegated.selectors_unavailable_reason,
            failure_locations=delegated.failure_locations,
            output_artifact_status=delegated.output_artifact_status,
            failure_expectation=delegated.failure_expectation,
            failure_chain_id=delegated.failure_chain_id,
            rerun_of_selectors=delegated.rerun_of_selectors,
        )

    def _from_profile(
        self,
        command: WorkspaceVerifyCommand,
        delegated: WorkspaceRunProfileResult | WorkspaceRunProfileBackgroundResult,
        reason: str,
        assessment: dict[str, object] | None,
        impact: dict[str, object] | None,
        recommendations: list[dict[str, object]],
        warning: str | None,
        *,
        head_sha: str,
        workspace_fingerprint: str,
        fallback_full: bool,
    ) -> WorkspaceVerifyResult:
        if isinstance(delegated, WorkspaceRunProfileBackgroundResult):
            return WorkspaceVerifyResult(
                summary="Workspace verification profile is running",
                next_action=delegated.safe_next_action,
                workspace_id=command.workspace_id,
                requested_mode=command.mode,
                selected_mode="profile",
                routing_reason=reason,
                impact_evidence=impact,
                assessment=assessment,
                recommendations=recommendations,
                staleness_warning=warning,
                operation={
                    "operation_id": delegated.operation_id,
                    "kind": "workspace_run_profile",
                    "state": "pending" if delegated.phase == "queued" else "running",
                    "phase": delegated.phase,
                    "progress_current": None,
                    "progress_total": None,
                    "cancellation_reason": None,
                    "poll_after_seconds": 1.0,
                },
                commands=[],
                steps=[],
                failed_step=None,
                failure_domain=None,
                business_tests_ran=False,
                valid_tdd_red_evidence=False,
                failure_reused=False,
                artifact_paths=[],
                outcome="running",
                satisfies_commit_gate=False,
                head_sha=head_sha,
                workspace_fingerprint=workspace_fingerprint,
            )
        return WorkspaceVerifyResult(
            summary=f"Verification profile {delegated.profile} passed",
            workspace_id=command.workspace_id,
            requested_mode=command.mode,
            selected_mode="profile",
            routing_reason=reason,
            impact_evidence=impact,
            assessment=assessment,
            recommendations=recommendations,
            staleness_warning=warning,
            operation=None,
            commands=[_command_evidence(item) for item in delegated.commands],
            steps=_profile_steps(delegated),
            failed_step=delegated.failed_step,
            failure_domain=delegated.failure_domain,
            business_tests_ran=delegated.business_tests_ran,
            valid_tdd_red_evidence=delegated.valid_tdd_red_evidence,
            failure_reused=False,
            artifact_paths=[],
            outcome="fallback_full" if fallback_full else "passed",
            satisfies_commit_gate=delegated.satisfies_commit_gate,
            head_sha=delegated.head_sha,
            workspace_fingerprint=delegated.fingerprint,
            execution_evidence=delegated.execution_evidence,
        )

    def _from_adhoc(
        self,
        command: WorkspaceVerifyCommand,
        delegated: WorkspaceRunAdhocResult | WorkspaceRunAdhocBackgroundResult,
        reason: str,
        assessment: dict[str, object] | None,
        impact: dict[str, object] | None,
        recommendations: list[dict[str, object]],
        warning: str | None,
        *,
        head_sha: str,
        workspace_fingerprint: str,
    ) -> WorkspaceVerifyResult:
        if isinstance(delegated, WorkspaceRunAdhocBackgroundResult):
            return WorkspaceVerifyResult(
                summary="Ad-hoc verification evidence is running",
                next_action=delegated.safe_next_action,
                workspace_id=command.workspace_id,
                requested_mode=command.mode,
                selected_mode="adhoc",
                routing_reason=reason,
                impact_evidence=impact,
                assessment=assessment,
                recommendations=recommendations,
                staleness_warning=warning,
                operation={
                    "operation_id": delegated.operation_id,
                    "kind": "workspace_run_adhoc",
                    "state": "pending" if delegated.phase == "queued" else "running",
                    "phase": delegated.phase,
                    "progress_current": None,
                    "progress_total": None,
                    "cancellation_reason": None,
                    "poll_after_seconds": 1.0,
                },
                commands=[],
                steps=[],
                failed_step=None,
                failure_domain=None,
                business_tests_ran=False,
                valid_tdd_red_evidence=False,
                failure_reused=False,
                artifact_paths=[],
                outcome="running",
                satisfies_commit_gate=False,
                head_sha=head_sha,
                workspace_fingerprint=workspace_fingerprint,
            )
        raw = {
            "argv": delegated.argv,
            "returncode": delegated.returncode,
            "duration_ms": delegated.duration_ms,
            "stdout": delegated.stdout,
            "stderr": delegated.stderr,
        }
        return WorkspaceVerifyResult(
            summary="Ad-hoc verification evidence completed",
            workspace_id=command.workspace_id,
            requested_mode=command.mode,
            selected_mode="adhoc",
            routing_reason=reason,
            impact_evidence=impact,
            assessment=assessment,
            recommendations=recommendations,
            staleness_warning=warning,
            operation=None,
            commands=[_command_evidence(raw)],
            steps=[],
            failed_step=None,
            failure_domain=None,
            business_tests_ran=False,
            valid_tdd_red_evidence=False,
            failure_reused=False,
            artifact_paths=[],
            outcome="passed" if delegated.returncode == 0 else "failed",
            satisfies_commit_gate=False,
            head_sha=delegated.head_sha,
            workspace_fingerprint=delegated.fingerprint_after,
            execution_evidence=delegated.execution_evidence,
            adhoc_evidence=_adhoc_evidence(delegated),
        )

    def _persist_artifact(
        self,
        result: WorkspaceVerifyResult,
        raw_path: str,
    ) -> WorkspaceVerifyResult:
        _, repo, workspace = self.ctx.workspace(result.workspace_id)
        relative_path = assert_path_allowed(raw_path, repo)
        target = resolve_workspace_path(workspace, relative_path, repo)
        if target.is_symlink():
            raise SecurityError("Verification artifact path cannot be a symlink")
        payload = sanitize_persisted_data(to_data(result))
        data = (json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode(
            "utf-8"
        )
        if len(data) > min(_MAX_ARTIFACT_BYTES, self.ctx.config.server.max_file_bytes):
            raise WorkspaceError("Verification artifact exceeds the reviewed file-size bound")
        with self.ctx.locks.lock(result.workspace_id):
            record = self.ctx.store.load(result.workspace_id)
            engine = open_file_transaction(self.ctx, workspace)
            engine.recover_pending()
            action = (
                WriteFile(relative_path, data, preserve_mode=True)
                if target.exists()
                else CreateFile(relative_path, data, 0o644)
            )
            engine.commit(TransactionPlan((action,)))
            record.last_verification = None
            self.ctx.store.save(record)
            fingerprint = prime_fingerprint(
                self.ctx.fingerprint_cache,
                result.workspace_id,
                self.ctx.git,
                workspace,
            ).fingerprint
        return replace(
            result,
            summary=f"{result.summary}; wrote verification artifact",
            artifact_paths=[relative_path],
            satisfies_commit_gate=False,
            workspace_fingerprint=fingerprint,
        )


__all__ = ["WorkspaceVerifier", "WorkspaceVerifyCommand", "WorkspaceVerifyResult"]
