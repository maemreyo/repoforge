"""Named, opt-in credential-scoping profiles for generic shell/ad-hoc execution (#381)."""

from __future__ import annotations

import pytest

from repoforge.domain.credential_profiles import (
    MAX_CREDENTIAL_PROFILES,
    available_credential_profiles,
    resolve_credential_profile_env,
    resolve_credential_profile_env_names,
    validate_credential_profiles,
)
from repoforge.domain.errors import ConfigError


def test_available_credential_profiles_is_a_stable_nonempty_catalog() -> None:
    profiles = available_credential_profiles()
    assert profiles
    assert len(set(profiles)) == len(profiles)
    assert available_credential_profiles() == profiles


def test_validate_credential_profiles_accepts_known_names() -> None:
    assert validate_credential_profiles(("docker", "cloud_aws"), "demo") == ("docker", "cloud_aws")


def test_validate_credential_profiles_rejects_unknown_name() -> None:
    with pytest.raises(ConfigError, match="unknown profile"):
        validate_credential_profiles(("not-a-real-profile",), "demo")


def test_validate_credential_profiles_rejects_duplicates() -> None:
    with pytest.raises(ConfigError, match="duplicates"):
        validate_credential_profiles(("docker", "docker"), "demo")


def test_validate_credential_profiles_rejects_too_many() -> None:
    too_many = tuple(available_credential_profiles()[0] for _ in range(MAX_CREDENTIAL_PROFILES + 1))
    with pytest.raises(ConfigError, match="must not exceed"):
        validate_credential_profiles(too_many, "demo")


def test_resolve_credential_profile_env_names_unions_and_dedupes() -> None:
    names = resolve_credential_profile_env_names(("docker", "cloud_aws"))
    assert "DOCKER_HOST" in names
    assert "AWS_ACCESS_KEY_ID" in names
    assert len(set(names)) == len(names)


def test_resolve_credential_profile_env_names_is_empty_for_no_profiles() -> None:
    assert resolve_credential_profile_env_names(()) == ()


def test_resolve_credential_profile_env_only_includes_names_present_in_the_source() -> None:
    """The allowlist-of-names orientation, not an exclusion list: a name the profile
    grants but that the host environment does not actually have set must not appear."""
    fake_environ = {"DOCKER_HOST": "unix:///var/run/docker.sock"}
    granted = resolve_credential_profile_env(("docker",), environ=fake_environ)
    assert granted == (("DOCKER_HOST", "unix:///var/run/docker.sock"),)


def test_resolve_credential_profile_env_never_reads_a_name_outside_the_profile() -> None:
    """A command cannot receive a credential-shaped variable that was not granted by
    an enrolled profile (#381 AC2), even if that variable happens to be set on the
    host -- only names the enrolled profiles actually list are ever candidates."""
    fake_environ = {
        "DOCKER_HOST": "unix:///var/run/docker.sock",
        "AWS_ACCESS_KEY_ID": "AKIAFAKEFAKEFAKEFAKE",
    }
    granted = resolve_credential_profile_env(("docker",), environ=fake_environ)
    granted_names = {name for name, _ in granted}
    assert "AWS_ACCESS_KEY_ID" not in granted_names


def test_resolve_credential_profile_env_is_empty_for_no_profiles_even_with_a_rich_environ() -> None:
    fake_environ = {"DOCKER_HOST": "unix:///var/run/docker.sock", "AWS_ACCESS_KEY_ID": "x"}
    assert resolve_credential_profile_env((), environ=fake_environ) == ()
