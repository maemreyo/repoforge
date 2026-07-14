from __future__ import annotations

from dataclasses import replace

from repoforge.application.workspace.risk import (
    assess_workspace_risk,
    recommend_verification,
)
from repoforge.domain.assessment import (
    AssessmentCoverage,
    AssessmentEvidenceStatus,
    WorkspaceAssessment,
    evidence,
    new_assessment_snapshot,
)
from repoforge.domain.risk import RiskLevel, default_risk_policy


def _assessment(paths: list[str], *, missing_ci: bool = False) -> WorkspaceAssessment:
    snapshot = new_assessment_snapshot(
        workspace_id="workspace-risk",
        head_sha="a" * 40,
        workspace_fingerprint="b" * 64,
        config_generation="c" * 64,
        policy_hash="d" * 64,
        created_at="2026-07-14T00:00:00+00:00",
    )

    def current(value: dict[str, object]):
        return evidence(
            snapshot,
            status=AssessmentEvidenceStatus.CURRENT,
            coverage=AssessmentCoverage.COMPLETE,
            value=value,
        )

    unavailable = evidence(
        snapshot,
        status=AssessmentEvidenceStatus.UNAVAILABLE,
        coverage=AssessmentCoverage.NONE,
        error_code="CI_UNAVAILABLE",
        safe_fallback="Treat CI as unknown.",
    )
    components = {
        "changed_paths": current({"paths": paths}),
        "diff_summary": current({"stat": "1 file changed", "truncated": False}),
        "change_budget": current(
            {
                "changed_files": len(paths),
                "within_limits": True,
                "diff_lines": 10,
                "limits": {"max_diff_lines": 1000},
            }
        ),
        "path_policy": current({"allowed_paths": paths, "violations": []}),
        "base_freshness": current({"behind_remote": 0, "remote_state": "current"}),
        "pr_state": current({"state": "OPEN"}),
        "ci_summary": (
            unavailable
            if missing_ci
            else current(
                {
                    "all_passed": True,
                    "pending": False,
                    "stale": False,
                    "summary": {"fail": 0},
                }
            )
        ),
        "failure_evidence_refs": (unavailable if missing_ci else current({"selectors": []})),
        "receipt_freshness": current({"last_verification": None}),
    }
    return WorkspaceAssessment(
        snapshot=snapshot,
        evidence_coverage={name: item.coverage.value for name, item in sorted(components.items())},
        uncertainties=(
            ("ci_summary:CI_UNAVAILABLE", "failure_evidence_refs:CI_UNAVAILABLE")
            if missing_ci
            else ()
        ),
        **components,
    )


def test_docs_only_risk_is_low_but_retains_final_profile() -> None:
    assessment = _assessment(["docs/guide.md"])
    policy = default_risk_policy(final_profile="full")
    risk = assess_workspace_risk(assessment, policy)
    recommendation = recommend_verification(
        assessment,
        risk,
        policy,
        available_profiles={"quick", "full"},
        available_diagnostics={"pytest-target"},
    )
    assert risk.level is RiskLevel.LOW
    assert recommendation.final_profile == "full"
    assert recommendation.required_profiles[-1] == "full"
    assert recommendation.manual_review_required is False


def test_security_contract_change_is_critical_and_explainable() -> None:
    assessment = _assessment(
        ["src/repoforge/security.py", "src/repoforge/interfaces/mcp/server.py"]
    )
    policy = default_risk_policy(final_profile="full")
    risk = assess_workspace_risk(assessment, policy)
    recommendation = recommend_verification(
        assessment,
        risk,
        policy,
        available_profiles={"full"},
        available_diagnostics=set(),
    )
    assert risk.level is RiskLevel.CRITICAL
    assert risk.public_contract_change is True
    assert risk.critical_paths
    assert all(item.reason and item.evidence_refs for item in risk.factors)
    assert recommendation.manual_review_required is True
    assert recommendation.ordered_stages[-1].profile == "full"


def test_missing_evidence_never_reduces_risk_or_verification() -> None:
    complete = _assessment(["src/repoforge/domain/policy.py"])
    missing = _assessment(["src/repoforge/domain/policy.py"], missing_ci=True)
    policy = default_risk_policy(final_profile="full")
    complete_risk = assess_workspace_risk(complete, policy)
    missing_risk = assess_workspace_risk(missing, policy)
    assert missing_risk.score >= complete_risk.score
    assert missing_risk.uncertainties
    recommendation = recommend_verification(
        missing,
        missing_risk,
        policy,
        available_profiles={"full"},
        available_diagnostics=set(),
    )
    assert recommendation.required_profiles == ("full",)
    assert any("evidence" in action.lower() for action in recommendation.next_safe_actions)


def test_risk_and_recommendation_become_stale_with_snapshot_change() -> None:
    assessment = _assessment(["docs/guide.md"])
    policy = default_risk_policy(final_profile="full")
    risk = assess_workspace_risk(assessment, policy)
    recommendation = recommend_verification(
        assessment,
        risk,
        policy,
        available_profiles={"full"},
        available_diagnostics=set(),
    )
    changed = replace(assessment.snapshot, snapshot_id="e" * 64)
    assert risk.assessment_snapshot_id != changed.snapshot_id
    assert recommendation.assessment_snapshot_id != changed.snapshot_id
