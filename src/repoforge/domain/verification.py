"""Pure verification-profile selection decisions."""

from __future__ import annotations

from ..config import ProfileConfig, RepositoryConfig
from .errors import ConfigError


def get_profile(repo: RepositoryConfig, profile_name: str) -> ProfileConfig:
    try:
        return repo.profiles[profile_name]
    except KeyError as exc:
        raise ConfigError(
            f"Unknown profile {profile_name!r}. Available: {sorted(repo.profiles)}"
        ) from exc


def select_verification_profile(
    repo: RepositoryConfig, profile_name: str | None
) -> tuple[ProfileConfig, bool]:
    selected = profile_name or repo.default_verification_profile
    if not selected:
        candidates = [name for name, profile in repo.profiles.items() if profile.verification]
        if len(candidates) == 1:
            selected = candidates[0]
        else:
            raise ConfigError(
                f"No default verification profile is configured. Available verification profiles: {sorted(candidates)}"
            )
    profile = get_profile(repo, selected)
    if not profile.verification:
        raise ConfigError(f"Profile {selected!r} is not a verification profile")
    return (profile, profile_name is None)
