"""Deterministic risk scoring and verification recommendation for one assessment snapshot."""

from __future__ import annotations

import fnmatch
from collections.abc import Collection

from ...domain.assessment import AssessmentEvidenceStatus, WorkspaceAssessment
from ...domain.risk import (
    RiskFactor,
    RiskLevel,
    RiskPolicy,
    VerificationRecommendation,
    VerificationStage,
    WorkspaceRiskAssessment,
)
from ...domain.verification import VerificationIntent


def _paths(assessment: WorkspaceAssessment) -> tuple[str, ...]:
    raw = assessment.changed_paths.value.get("paths", ())
    if not isinstance(raw, list):
        return ()
    return tuple(sorted({item.replace("\\", "/") for item in raw if isinstance(item, str)}))


def _matches(path: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns)


def _factor(
    code: str,
    weight: int,
    reason: str,
    *evidence_refs: str,
) -> RiskFactor:
    return RiskFactor(code, weight, reason, tuple(sorted(set(evidence_refs))))


def _level(score: int, policy: RiskPolicy) -> RiskLevel:
    if score <= policy.low_max:
        return RiskLevel.LOW
    if score <= policy.medium_max:
        return RiskLevel.MEDIUM
    if score <= policy.high_max:
        return RiskLevel.HIGH
    return RiskLevel.CRITICAL


def assess_workspace_risk(
    assessment: WorkspaceAssessment,
    policy: RiskPolicy,
) -> WorkspaceRiskAssessment:
    paths = _paths(assessment)
    factors: list[RiskFactor] = []
    uncertainties = tuple(sorted(set(assessment.uncertainties)))
    critical_paths = tuple(path for path in paths if _matches(path, policy.critical_globs))
    manifest_paths = tuple(path for path in paths if _matches(path, policy.manifest_globs))
    public_paths = tuple(path for path in paths if _matches(path, policy.public_contract_globs))
    docs_only = bool(paths) and all(_matches(path, policy.docs_globs) for path in paths)

    if not paths:
        factors.append(
            _factor(
                "NO_CHANGED_PATH_EVIDENCE",
                20,
                "No changed path evidence is available, so the assessment cannot assume low risk.",
                "changed_paths",
            )
        )
    elif docs_only:
        factors.append(
            _factor(
                "DOCS_ONLY_CHANGE",
                5,
                "Every changed path matches the configured documentation-only policy.",
                "changed_paths",
                "path_policy",
            )
        )
    else:
        factors.append(
            _factor(
                "SOURCE_CHANGE",
                15,
                "At least one changed path is outside the documentation-only policy.",
                "changed_paths",
                "diff_summary",
            )
        )

    if critical_paths:
        factors.append(
            _factor(
                "CRITICAL_PATH_CHANGE",
                min(60, 40 + 5 * len(critical_paths)),
                "Changes touch configured security, runtime, configuration, schema, or release-gate paths.",
                "changed_paths",
                "path_policy",
            )
        )
    if public_paths:
        factors.append(
            _factor(
                "PUBLIC_CONTRACT_CHANGE",
                25,
                "Changes touch configured public CLI, MCP, configuration, or error-contract paths.",
                "changed_paths",
            )
        )
    if manifest_paths:
        factors.append(
            _factor(
                "DEPENDENCY_OR_MANIFEST_CHANGE",
                20,
                "Dependency or build manifest changes can alter the reproducible execution surface.",
                "changed_paths",
                "receipt_freshness",
            )
        )
    if len(paths) >= 8:
        factors.append(
            _factor(
                "BROAD_CHANGESET",
                min(20, 8 + len(paths) // 2),
                "The change spans enough files to increase integration and omission risk.",
                "changed_paths",
                "change_budget",
            )
        )

    budget = assessment.change_budget.value
    diff_lines = budget.get("diff_lines")
    limits = budget.get("limits")
    if isinstance(diff_lines, int) and isinstance(limits, dict):
        maximum = limits.get("max_diff_lines")
        if isinstance(maximum, int) and maximum > 0 and diff_lines * 100 >= maximum * 80:
            factors.append(
                _factor(
                    "CHANGE_BUDGET_PRESSURE",
                    15,
                    "Diff size is at least eighty percent of the configured change budget.",
                    "change_budget",
                )
            )
    if budget.get("within_limits") is False:
        factors.append(
            _factor(
                "CHANGE_BUDGET_EXCEEDED",
                35,
                "The workspace exceeds its configured change budget.",
                "change_budget",
            )
        )

    base = assessment.base_freshness.value
    behind = base.get("behind_remote")
    if isinstance(behind, int) and behind > 0:
        factors.append(
            _factor(
                "STALE_BASE",
                min(25, 10 + behind),
                "The workspace base is behind the reviewed remote base.",
                "base_freshness",
            )
        )

    ci = assessment.ci_summary.value
    summary = ci.get("summary")
    failures = summary.get("fail") if isinstance(summary, dict) else None
    if ci.get("all_passed") is False or (isinstance(failures, int) and failures > 0):
        factors.append(
            _factor(
                "FAILING_CI",
                35,
                "Current pull-request checks contain a failure or are not all passing.",
                "ci_summary",
                "failure_evidence_refs",
            )
        )
    elif ci.get("pending") is True or ci.get("stale") is True:
        factors.append(
            _factor(
                "INCOMPLETE_CI",
                15,
                "CI is pending or tied to stale commit evidence.",
                "ci_summary",
            )
        )

    receipt = assessment.receipt_freshness.value.get("last_verification")
    if isinstance(receipt, dict) and receipt.get("fingerprint_matches") is False:
        factors.append(
            _factor(
                "STALE_VERIFICATION_RECEIPT",
                20,
                "The latest verification receipt does not match the current workspace fingerprint.",
                "receipt_freshness",
            )
        )

    if uncertainties:
        factors.append(
            _factor(
                "MISSING_OR_PARTIAL_EVIDENCE",
                min(35, 15 + 5 * len(uncertainties)),
                "Missing or partial assessment evidence broadens risk rather than reducing it.",
                *tuple(item.split(":", 1)[0] for item in uncertainties),
            )
        )
    for name, component in (
        ("changed_paths", assessment.changed_paths),
        ("diff_summary", assessment.diff_summary),
        ("change_budget", assessment.change_budget),
        ("path_policy", assessment.path_policy),
        ("base_freshness", assessment.base_freshness),
        ("ci_summary", assessment.ci_summary),
        ("receipt_freshness", assessment.receipt_freshness),
    ):
        if component.status is not AssessmentEvidenceStatus.CURRENT and not any(
            item.startswith(f"{name}:") for item in uncertainties
        ):
            uncertainties = tuple(sorted((*uncertainties, f"{name}:{component.status.value}")))

    score = min(100, sum(item.weight for item in factors))
    return WorkspaceRiskAssessment(
        assessment_snapshot_id=assessment.snapshot.snapshot_id,
        score=score,
        level=_level(score, policy),
        factors=tuple(sorted(factors, key=lambda item: (item.code, item.reason))),
        uncertainties=uncertainties,
        critical_paths=critical_paths,
        manifest_paths=manifest_paths,
        public_contract_change=bool(public_paths),
    )


def recommend_verification(
    assessment: WorkspaceAssessment,
    risk: WorkspaceRiskAssessment,
    policy: RiskPolicy,
    *,
    available_profiles: Collection[str],
    available_diagnostics: Collection[str],
    intent: VerificationIntent | str | None = None,
) -> VerificationRecommendation:
    if risk.assessment_snapshot_id != assessment.snapshot.snapshot_id:
        raise ValueError("risk belongs to a different assessment snapshot")

    normalized_intent = VerificationIntent.parse(intent)
    profiles: list[str] = []
    stages: list[VerificationStage] = []
    diagnostics = tuple(
        item
        for item in policy.narrow_diagnostics
        if item in available_diagnostics
        and (
            normalized_intent.prefers_narrow_diagnostics
            or risk.level in {RiskLevel.LOW, RiskLevel.MEDIUM}
        )
    )
    for diagnostic in diagnostics:
        stages.append(
            VerificationStage(
                order=len(stages) + 1,
                kind="diagnostic",
                diagnostic=diagnostic,
                reason="Run the narrowest configured read-only diagnostic first.",
            )
        )

    if risk.level in {RiskLevel.LOW, RiskLevel.MEDIUM}:
        for profile in policy.ordered_profiles:
            if (
                profile in available_profiles
                and profile != policy.final_profile
                and profile not in profiles
            ):
                profiles.append(profile)
                break
    elif risk.level is RiskLevel.HIGH:
        for profile in policy.ordered_profiles:
            if (
                profile in available_profiles
                and profile != policy.final_profile
                and profile not in profiles
            ):
                profiles.append(profile)

    final_profile = policy.final_profile
    if final_profile not in available_profiles and available_profiles:
        final_profile = sorted(available_profiles)[-1]
    if final_profile not in profiles:
        profiles.append(final_profile)
    for profile in profiles:
        stages.append(
            VerificationStage(
                order=len(stages) + 1,
                kind="profile",
                profile=profile,
                reason=(
                    "Run the configured final verification gate on the exact current tree."
                    if profile == final_profile
                    else "Run an available earlier verification profile before the final gate."
                ),
            )
        )

    actions: list[str] = []
    if risk.uncertainties:
        actions.append(
            "Collect or restore missing assessment evidence before relying on the result."
        )
    if risk.critical_paths:
        actions.append("Review critical-path changes manually before commit or publication.")
    if risk.public_contract_change:
        actions.append("Review compatibility and release-contract impact explicitly.")
    actions.append(
        f"Run the final verification profile `{final_profile}` on the exact current tree."
    )

    return VerificationRecommendation(
        assessment_snapshot_id=assessment.snapshot.snapshot_id,
        ordered_stages=tuple(stages),
        required_profiles=tuple(profiles),
        recommended_diagnostics=diagnostics,
        final_profile=final_profile,
        manual_review_required=risk.level is RiskLevel.CRITICAL,
        next_safe_actions=tuple(actions),
    )
