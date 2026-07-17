"""Explainable deterministic workspace risk and verification contracts."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass(frozen=True, slots=True)
class RiskFactor:
    code: str
    weight: int
    reason: str
    evidence_refs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RiskPolicy:
    low_max: int
    medium_max: int
    high_max: int
    critical_globs: tuple[str, ...]
    public_contract_globs: tuple[str, ...]
    manifest_globs: tuple[str, ...]
    docs_globs: tuple[str, ...]
    narrow_diagnostics: tuple[str, ...]
    ordered_profiles: tuple[str, ...]
    final_profile: str

    def __post_init__(self) -> None:
        if not 0 <= self.low_max < self.medium_max < self.high_max <= 100:
            raise ValueError("risk thresholds must be strictly increasing within 0..100")
        if not self.final_profile:
            raise ValueError("final_profile is required")
        if len(self.critical_globs) > 64 or len(self.public_contract_globs) > 64:
            raise ValueError("risk path policy is too large")


@dataclass(frozen=True, slots=True)
class WorkspaceRiskAssessment:
    assessment_snapshot_id: str
    score: int
    level: RiskLevel
    factors: tuple[RiskFactor, ...]
    uncertainties: tuple[str, ...]
    critical_paths: tuple[str, ...]
    manifest_paths: tuple[str, ...]
    public_contract_change: bool


@dataclass(frozen=True, slots=True)
class VerificationStage:
    order: int
    kind: str
    reason: str
    profile: str | None = None
    diagnostic: str | None = None
    selector: str | None = None


@dataclass(frozen=True, slots=True)
class VerificationRecommendation:
    assessment_snapshot_id: str
    ordered_stages: tuple[VerificationStage, ...]
    required_profiles: tuple[str, ...]
    recommended_diagnostics: tuple[str, ...]
    final_profile: str
    manual_review_required: bool
    next_safe_actions: tuple[str, ...]


def default_risk_policy(*, final_profile: str) -> RiskPolicy:
    return RiskPolicy(
        low_max=24,
        medium_max=49,
        high_max=74,
        critical_globs=(
            ".github/workflows/**",
            "**/auth*.py",
            "**/config.py",
            "**/runtime*.py",
            "**/schema*.py",
            "**/security*.py",
            "scripts/verify-production.sh",
        ),
        public_contract_globs=(
            "src/repoforge/config.py",
            "src/repoforge/domain/errors.py",
            "src/repoforge/interfaces/cli/**",
            "src/repoforge/interfaces/mcp/**",
        ),
        manifest_globs=(
            "**/package-lock.json",
            "**/pnpm-lock.yaml",
            "**/poetry.lock",
            "**/uv.lock",
            "Cargo.lock",
            "pyproject.toml",
        ),
        docs_globs=("*.md", "docs/**"),
        narrow_diagnostics=("pytest-target",),
        ordered_profiles=("quick", final_profile),
        final_profile=final_profile,
    )
