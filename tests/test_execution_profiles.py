"""Built-in named execution-environment profiles (#380)."""

from __future__ import annotations

import pytest

from repoforge.domain.errors import ConfigError
from repoforge.domain.execution_profiles import (
    MAX_EXECUTION_PROFILES,
    available_execution_profiles,
    resolve_execution_profile_runners,
    validate_execution_profiles,
)


def test_available_execution_profiles_is_a_stable_nonempty_catalog() -> None:
    profiles = available_execution_profiles()
    assert profiles
    assert len(set(profiles)) == len(profiles)
    assert available_execution_profiles() == profiles


def test_validate_execution_profiles_accepts_known_names() -> None:
    assert validate_execution_profiles(("python", "node"), "demo") == ("python", "node")


def test_validate_execution_profiles_rejects_unknown_name() -> None:
    with pytest.raises(ConfigError, match="unknown profile"):
        validate_execution_profiles(("not-a-real-profile",), "demo")


def test_validate_execution_profiles_rejects_duplicates() -> None:
    with pytest.raises(ConfigError, match="duplicates"):
        validate_execution_profiles(("python", "python"), "demo")


def test_validate_execution_profiles_rejects_too_many() -> None:
    too_many = tuple(available_execution_profiles()[0] for _ in range(MAX_EXECUTION_PROFILES + 1))
    with pytest.raises(ConfigError, match="must not exceed"):
        validate_execution_profiles(too_many, "demo")


def test_resolve_execution_profile_runners_unions_and_dedupes() -> None:
    # "git" is a runner both "git" and "github" profiles contribute.
    runners = resolve_execution_profile_runners(("git", "github"))
    assert runners.count("git") == 1
    assert "gh" in runners


def test_resolve_execution_profile_runners_is_empty_for_no_profiles() -> None:
    assert resolve_execution_profile_runners(()) == ()


def test_resolve_execution_profile_runners_ignores_unknown_name() -> None:
    """Projection of an already-validated value never raises -- validation is
    validate_execution_profiles' job, not this function's."""
    assert resolve_execution_profile_runners(("not-a-real-profile",)) == ()
