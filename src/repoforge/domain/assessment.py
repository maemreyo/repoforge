"""Snapshot-consistent workspace assessment models and invariants."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any

from .errors import ErrorCode, RepoForgeError
from .risk import VerificationRecommendation, WorkspaceRiskAssessment

_SHA40 = re.compile(r"^[a-f0-9]{40}$")
_SHA64 = re.compile(r"^[a-f0-9]{64}$")
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_MAX_UNCERTAINTIES = 64
_MAX_COMPONENTS = 32


class AssessmentEvidenceStatus(str, Enum):
    CURRENT = "current"
    PARTIAL = "partial"
    UNAVAILABLE = "unavailable"
    NOT_APPLICABLE = "not_applicable"


class AssessmentCoverage(str, Enum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    NONE = "none"


@dataclass(frozen=True, slots=True)
class AssessmentSnapshot:
    snapshot_id: str
    workspace_id: str
    head_sha: str
    workspace_fingerprint: str
    config_generation: str
    policy_hash: str
    created_at: str


@dataclass(frozen=True, slots=True)
class AssessmentEvidence:
    snapshot_id: str
    status: AssessmentEvidenceStatus
    coverage: AssessmentCoverage
    value: dict[str, Any]
    error_code: str | None = None
    safe_fallback: str | None = None


@dataclass(frozen=True, slots=True)
class WorkspaceAssessment:
    snapshot: AssessmentSnapshot
    changed_paths: AssessmentEvidence
    diff_summary: AssessmentEvidence
    change_budget: AssessmentEvidence
    path_policy: AssessmentEvidence
    base_freshness: AssessmentEvidence
    pr_state: AssessmentEvidence
    ci_summary: AssessmentEvidence
    failure_evidence_refs: AssessmentEvidence
    receipt_freshness: AssessmentEvidence
    evidence_coverage: dict[str, str]
    uncertainties: tuple[str, ...]
    current: bool = True
    risk: WorkspaceRiskAssessment | None = None
    verification_recommendation: VerificationRecommendation | None = None


def _invalid(message: str) -> RepoForgeError:
    return RepoForgeError(
        message,
        code=ErrorCode.ASSESSMENT_INVALID,
        safe_next_action="Rebuild the assessment from the latest workspace identity.",
    )


def assessment_snapshot_id(
    *,
    workspace_id: str,
    head_sha: str,
    workspace_fingerprint: str,
    config_generation: str,
    policy_hash: str,
    created_at: str,
) -> str:
    payload = {
        "workspace_id": workspace_id,
        "head_sha": head_sha,
        "workspace_fingerprint": workspace_fingerprint,
        "config_generation": config_generation,
        "policy_hash": policy_hash,
        "created_at": created_at,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def new_assessment_snapshot(
    *,
    workspace_id: str,
    head_sha: str,
    workspace_fingerprint: str,
    config_generation: str,
    policy_hash: str,
    created_at: str,
) -> AssessmentSnapshot:
    if _SAFE_ID.fullmatch(workspace_id) is None:
        raise _invalid("workspace_id has an invalid format")
    for name, value, pattern in (
        ("head_sha", head_sha, _SHA40),
        ("workspace_fingerprint", workspace_fingerprint, _SHA64),
        ("config_generation", config_generation, _SHA64),
        ("policy_hash", policy_hash, _SHA64),
    ):
        if pattern.fullmatch(value) is None:
            raise _invalid(f"{name} has an invalid format")
    snapshot_id = assessment_snapshot_id(
        workspace_id=workspace_id,
        head_sha=head_sha,
        workspace_fingerprint=workspace_fingerprint,
        config_generation=config_generation,
        policy_hash=policy_hash,
        created_at=created_at,
    )
    return AssessmentSnapshot(
        snapshot_id=snapshot_id,
        workspace_id=workspace_id,
        head_sha=head_sha,
        workspace_fingerprint=workspace_fingerprint,
        config_generation=config_generation,
        policy_hash=policy_hash,
        created_at=created_at,
    )


def evidence(
    snapshot: AssessmentSnapshot,
    *,
    status: AssessmentEvidenceStatus,
    coverage: AssessmentCoverage,
    value: dict[str, Any] | None = None,
    error_code: str | None = None,
    safe_fallback: str | None = None,
) -> AssessmentEvidence:
    if status is AssessmentEvidenceStatus.CURRENT and coverage is not AssessmentCoverage.COMPLETE:
        raise _invalid("current evidence must have complete coverage")
    if coverage is AssessmentCoverage.NONE and value:
        raise _invalid("evidence with no coverage cannot contain a value")
    if (
        status
        in {
            AssessmentEvidenceStatus.PARTIAL,
            AssessmentEvidenceStatus.UNAVAILABLE,
        }
        and not error_code
    ):
        raise _invalid("partial or unavailable evidence requires a stable error code")
    return AssessmentEvidence(
        snapshot_id=snapshot.snapshot_id,
        status=status,
        coverage=coverage,
        value={} if value is None else value,
        error_code=error_code,
        safe_fallback=safe_fallback,
    )


def validate_workspace_assessment(assessment: WorkspaceAssessment) -> WorkspaceAssessment:
    components = {
        "changed_paths": assessment.changed_paths,
        "diff_summary": assessment.diff_summary,
        "change_budget": assessment.change_budget,
        "path_policy": assessment.path_policy,
        "base_freshness": assessment.base_freshness,
        "pr_state": assessment.pr_state,
        "ci_summary": assessment.ci_summary,
        "failure_evidence_refs": assessment.failure_evidence_refs,
        "receipt_freshness": assessment.receipt_freshness,
    }
    if len(components) > _MAX_COMPONENTS:
        raise _invalid("assessment contains too many evidence components")
    for name, component in components.items():
        if component.snapshot_id != assessment.snapshot.snapshot_id:
            raise _invalid(f"{name} belongs to a different assessment snapshot")
        if assessment.evidence_coverage.get(name) != component.coverage.value:
            raise _invalid(f"{name} coverage summary does not match component coverage")
    if set(assessment.evidence_coverage) != set(components):
        raise _invalid("evidence_coverage must contain every component exactly once")
    if len(assessment.uncertainties) > _MAX_UNCERTAINTIES:
        raise _invalid("assessment contains too many uncertainties")
    if tuple(sorted(set(assessment.uncertainties))) != assessment.uncertainties:
        raise _invalid("uncertainties must be sorted and unique")
    risk = assessment.risk
    recommendation = assessment.verification_recommendation
    if (risk is None) != (recommendation is None):
        raise _invalid("risk and verification recommendation must be present together")
    if risk is not None and recommendation is not None:
        if risk.assessment_snapshot_id != assessment.snapshot.snapshot_id:
            raise _invalid("risk belongs to a different assessment snapshot")
        if recommendation.assessment_snapshot_id != assessment.snapshot.snapshot_id:
            raise _invalid("verification recommendation belongs to a different snapshot")
        if len(risk.factors) > 64 or len(risk.uncertainties) > _MAX_UNCERTAINTIES:
            raise _invalid("risk contains too many factors or uncertainties")
        if len(recommendation.ordered_stages) > 32:
            raise _invalid("verification recommendation contains too many stages")
        if len(recommendation.next_safe_actions) > 32:
            raise _invalid("verification recommendation contains too many actions")
        orders = tuple(stage.order for stage in recommendation.ordered_stages)
        if orders != tuple(range(1, len(orders) + 1)):
            raise _invalid("verification stages must have contiguous one-based order")
        if not recommendation.required_profiles:
            raise _invalid("verification recommendation must retain a final profile")
        if recommendation.required_profiles[-1] != recommendation.final_profile:
            raise _invalid("final verification profile must be the last required profile")
    if not assessment.current:
        raise _invalid("a returned workspace assessment must be current")
    return assessment
