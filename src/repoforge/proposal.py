"""Pure repository-enrollment proposal assessment."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .discovery import RepositoryDetection


class ProposalConfidence(str, Enum):
    """Confidence in detection facts, never permission to execute discovered commands."""

    HIGH = "high"
    MEDIUM = "medium"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class RequiredDecision:
    """An enrollment choice that cannot safely be inferred from local facts."""

    code: str
    prompt: str
    choices: tuple[str, ...]


@dataclass(frozen=True)
class ProposalAssessment:
    """Deterministic confidence and operator decisions for one repository proposal."""

    confidence: ProposalConfidence
    findings: tuple[str, ...]
    decisions: tuple[RequiredDecision, ...]


def assess_repository_proposal(detection: RepositoryDetection) -> ProposalAssessment:
    """Classify repository facts without executing candidate commands."""
    findings = list(detection.warnings)
    decisions: list[RequiredDecision] = []
    if detection.ecosystem == "generic" or not detection.profiles:
        decisions.append(
            RequiredDecision(
                code="verification_profiles",
                prompt="No supported verification profile was detected. Choose enrollment mode.",
                choices=("read_only", "manual_policy"),
            )
        )
        return ProposalAssessment(ProposalConfidence.BLOCKED, tuple(findings), tuple(decisions))
    if detection.warnings:
        return ProposalAssessment(ProposalConfidence.MEDIUM, tuple(findings), tuple(decisions))
    return ProposalAssessment(ProposalConfidence.HIGH, tuple(findings), tuple(decisions))
