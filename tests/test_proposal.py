from __future__ import annotations

from pathlib import Path

from repoforge.discovery import DetectedProfile, RepositoryDetection
from repoforge.proposal import ProposalConfidence, assess_repository_proposal


def _detection(
    *, ecosystem: str, profiles: tuple[DetectedProfile, ...], warnings: tuple[str, ...] = ()
) -> RepositoryDetection:
    return RepositoryDetection(
        path=Path("/tmp/demo"),
        repo_id="demo",
        display_name="demo",
        remote="origin",
        default_base="main",
        ecosystem=ecosystem,
        package_manager="python" if ecosystem == "python" else None,
        package_manager_version=None,
        scripts=(),
        instruction_files=(),
        profiles=profiles,
        warnings=warnings,
    )


def test_assessment_is_high_for_supported_profile_without_warning() -> None:
    # Given: a supported repository with a detected verification profile.
    detection = _detection(
        ecosystem="python",
        profiles=(DetectedProfile("test", "tests", True, (("python", "-m", "pytest"),)),),
    )

    # When: enrollment facts are assessed.
    assessment = assess_repository_proposal(detection)

    # Then: facts are high confidence but still require approval at enrollment.
    assert assessment.confidence is ProposalConfidence.HIGH
    assert assessment.decisions == ()


def test_assessment_blocks_unsupported_repository_without_profile() -> None:
    # Given: a generic repository with no generated verification capability.
    detection = _detection(ecosystem="generic", profiles=(), warnings=("No manifest",))

    # When: enrollment facts are assessed.
    assessment = assess_repository_proposal(detection)

    # Then: it requires an explicit read-only or manual-policy choice.
    assert assessment.confidence is ProposalConfidence.BLOCKED
    assert assessment.decisions[0].choices == ("read_only", "manual_policy")
