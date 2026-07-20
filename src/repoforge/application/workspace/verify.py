"""Unified planning and execution orchestration for workspace verification."""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from typing import Any, Literal

from ...domain.diagnostics import DiagnosticExpectation, DiagnosticFailureClass
from ...domain.errors import ConfigError, SecurityError, WorkspaceError
from ...domain.filesystem_transaction import CreateFile, TransactionPlan, WriteFile
from ...domain.policy import assert_path_allowed, resolve_workspace_path
from ...domain.redaction import sanitize_persisted_data
from ...domain.verification import VerificationIntent
from ..context import ApplicationContext
from ..dto import to_data
from ..file_transactions import open_file_transaction
from ..fingerprint_cache import prime_fingerprint
from .assessment import WorkspaceAssessmentCommand, WorkspaceAssessmentReader
from .diagnostic_selector import SelectorInput
from .run_adhoc import (
    WorkspaceAdhocRunner,
    WorkspaceRunAdhocBackgroundResult,
    WorkspaceRunAdhocCommand,
    WorkspaceRunAdhocResult,
)
from .run_diagnostic import (
    WorkspaceDiagnosticRunner,
    WorkspaceRunDiagnosticCommand,
    WorkspaceRunDiagnosticResult,
)
from .run_profile import (
    WorkspaceProfileRunner,
    WorkspaceRunProfileBackgroundResult,
    WorkspaceRunProfileCommand,
    WorkspaceRunProfileResult,
)

VerifyMode = Literal["plan", "auto", "diagnostic", "profile", "adhoc"]
VerifyRerun = Literal["failed"]
_HIGH_CONFIDENCE = 95
_MAX_ARTIFACT_BYTES = 120_000


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
    background: bool = False
    intent: VerificationIntent | str | None = None
    expectation: DiagnosticExpectation | str | None = None
    expected_failure_class: DiagnosticFailureClass | str | None = None
    force_rerun: bool = False
    rerun: VerifyRerun | None = None
    impact_paths: tuple[str, ...] = ()
    artifact_output_path: str | None = None


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
    failed_selectors: list[str] = field(default_factory=list)
    output_artifact_reference: str | None = None
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
        "output_excerpt": excerpt[:12_000],
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
        profile: WorkspaceProfileRunner,
        diagnostic: WorkspaceDiagnosticRunner,
        adhoc: WorkspaceAdhocRunner,
    ) -> None:
        self.ctx = ctx
        self._assessment = assessment
        self._profile = profile
        self._diagnostic = diagnostic
        self._adhoc = adhoc

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
        assessment = self._assessment.execute(
            WorkspaceAssessmentCommand(command.workspace_id, command.impact_paths)
        )
        if (
            command.expected_fingerprint is not None
            and command.expected_fingerprint != assessment.snapshot.workspace_fingerprint
        ):
            raise WorkspaceError(
                "Workspace changed since the requested verification snapshot was reviewed"
            )
        assessment_projection, impact_evidence = _assessment_projection(assessment)
        recommendations = _recommendations(assessment)
        warning = _staleness_warning(assessment)
        recommendation = assessment.verification_recommendation
        if recommendation is None:
            raise WorkspaceError("Workspace assessment did not produce a final profile")
        final_profile = recommendation.final_profile

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
                head_sha=assessment.snapshot.head_sha,
                workspace_fingerprint=assessment.snapshot.workspace_fingerprint,
            )

        selected_mode = command.mode
        routing_reason = f"Explicit {command.mode} mode was requested."
        fallback_full = False
        diagnostic_id = command.diagnostic_id
        selector: SelectorInput = command.selector
        profile_name = command.profile_name
        if command.mode == "auto":
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
            diagnostic_result = self._diagnostic.execute(
                WorkspaceRunDiagnosticCommand(
                    workspace_id=command.workspace_id,
                    diagnostic_id=diagnostic_id,
                    selector=selector,
                    expected_fingerprint=assessment.snapshot.workspace_fingerprint,
                    intent=command.intent,
                    expectation=command.expectation,
                    expected_failure_class=command.expected_failure_class,
                    selector2=command.selector2,
                    force_rerun=command.force_rerun,
                    rerun_failed=command.rerun == "failed",
                )
            )
            result = self._from_diagnostic(
                command,
                diagnostic_result,
                routing_reason,
                assessment_projection,
                impact_evidence,
                recommendations,
                warning,
            )
        elif selected_mode == "profile":
            profile_result = self._profile.execute(
                WorkspaceRunProfileCommand(
                    command.workspace_id,
                    profile_name,
                    command.background,
                    command.force_rerun,
                    assessment.snapshot.workspace_fingerprint,
                )
            )
            result = self._from_profile(
                command,
                profile_result,
                routing_reason,
                assessment_projection,
                impact_evidence,
                recommendations,
                warning,
                head_sha=assessment.snapshot.head_sha,
                workspace_fingerprint=assessment.snapshot.workspace_fingerprint,
                fallback_full=fallback_full,
            )
        else:
            if command.argv is None:
                raise ConfigError("adhoc mode requires argv")
            adhoc_result = self._adhoc.execute(
                WorkspaceRunAdhocCommand(
                    command.workspace_id,
                    command.argv,
                    command.working_directory,
                    command.background,
                    assessment.snapshot.workspace_fingerprint,
                )
            )
            result = self._from_adhoc(
                command,
                adhoc_result,
                routing_reason,
                assessment_projection,
                impact_evidence,
                recommendations,
                warning,
                head_sha=assessment.snapshot.head_sha,
                workspace_fingerprint=assessment.snapshot.workspace_fingerprint,
            )

        if command.artifact_output_path is not None:
            return self._persist_artifact(result, command.artifact_output_path)
        return result

    def _from_diagnostic(
        self,
        command: WorkspaceVerifyCommand,
        delegated: WorkspaceRunDiagnosticResult,
        reason: str,
        assessment: dict[str, object],
        impact: dict[str, object] | None,
        recommendations: list[dict[str, object]],
        warning: str | None,
    ) -> WorkspaceVerifyResult:
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
            failure_expectation=delegated.failure_expectation,
            failure_chain_id=delegated.failure_chain_id,
            rerun_of_selectors=delegated.rerun_of_selectors,
        )

    def _from_profile(
        self,
        command: WorkspaceVerifyCommand,
        delegated: WorkspaceRunProfileResult | WorkspaceRunProfileBackgroundResult,
        reason: str,
        assessment: dict[str, object],
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
                    "state": "running",
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
        assessment: dict[str, object],
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
                    "state": "running",
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
