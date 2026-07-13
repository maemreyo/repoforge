from dataclasses import replace

import pytest

from repoforge.domain.errors import ConfigError, ErrorCode
from repoforge.domain.onboarding import (
    DiscoveryCandidate,
    DiscoveryIdentity,
    OnboardingOptions,
    OnboardingRepositoryState,
    OnboardingSession,
    OnboardingStatus,
    RepositoryProgress,
    detect_duplicate_repo_ids,
    summarize_session,
    transition_session,
)


def candidate(repo_id: str, path: str) -> DiscoveryCandidate:
    return DiscoveryCandidate(DiscoveryIdentity(path, path, f"{path}/.git", True, False), repo_id)


def test_duplicate_repository_ids_report_all_paths() -> None:
    assert detect_duplicate_repo_ids((candidate("api", "/a/api"), candidate("api", "/b/api"))) == {
        "api": ("/a/api", "/b/api")
    }


def test_session_transition_rejects_skipping_required_states() -> None:
    session = OnboardingSession.new(
        session_id="a" * 24,
        created_at="now",
        config_path="/tmp/c",
        roots=("/repos",),
        options=OnboardingOptions(),
    )
    with pytest.raises(ValueError, match="created -> ready"):
        transition_session(session, OnboardingStatus.READY, now="later")


def test_summary_counts_repository_progress() -> None:
    session = OnboardingSession.new(
        session_id="b" * 24,
        created_at="now",
        config_path="/tmp/c",
        roots=("/repos",),
        options=OnboardingOptions(),
    )
    session = replace(
        session,
        repositories=(
            OnboardingRepositoryState(candidate("one", "/one"), RepositoryProgress.APPROVED),
            OnboardingRepositoryState(candidate("two", "/two"), RepositoryProgress.SKIPPED),
        ),
    )
    summary = summarize_session(session)
    assert summary.approved == 1 and summary.skipped == 1


def test_onboarding_error_codes_are_stable() -> None:
    assert ErrorCode.SESSION_STALE.value == "SESSION_STALE"
    assert ConfigError("SESSION_STALE: changed").code is ErrorCode.SESSION_STALE
