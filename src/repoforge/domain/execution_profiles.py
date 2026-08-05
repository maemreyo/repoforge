"""Built-in named execution-environment profiles (#380).

A profile is a reviewed, named grouping of runner basenames for a common developer
toolchain -- enrolling a repository in "python" or "node" is what lets an operator select
a reviewed developer environment without manually allowlisting every individual command
(AC1), instead of enumerating uv/pytest/ruff/mypy or node/npm/pnpm one at a time in
adhoc_runners. Profiles are additive to adhoc_runners, never a replacement: a repository's
effective runner allowlist (RepositoryConfig.effective_adhoc_runners) is the union of its
own adhoc_runners plus every runner named by its enrolled execution_profiles.

The catalog is fixed, not operator-authored: an unknown profile name is always a typo or a
stale reference, never a legitimate custom profile. A repository-specific tool the catalog
does not cover still belongs directly in adhoc_runners.
"""

from __future__ import annotations

from .errors import ConfigError

MAX_EXECUTION_PROFILES = 16

_BUILTIN_EXECUTION_PROFILES: dict[str, tuple[str, ...]] = {
    "git": ("git",),
    "github": ("git", "gh"),
    "python": ("python3", "uv", "pip", "pytest", "ruff", "mypy"),
    "node": ("node", "npm", "pnpm", "npx"),
    "docker": ("docker", "docker-compose"),
    "cloud": ("aws", "gcloud", "az"),
}


def available_execution_profiles() -> tuple[str, ...]:
    """The reviewed profile catalog, in a stable order -- for operator-facing discovery,
    e.g. naming the valid choices in a config error."""
    return tuple(_BUILTIN_EXECUTION_PROFILES)


def validate_execution_profiles(profiles: tuple[str, ...], repo_id: str) -> tuple[str, ...]:
    """Validate a repository's enrolled profile names at config-load time."""
    if len(profiles) > MAX_EXECUTION_PROFILES:
        raise ConfigError(
            f"repositories.{repo_id}.execution_profiles must not exceed "
            f"{MAX_EXECUTION_PROFILES} entries"
        )
    if len(set(profiles)) != len(profiles):
        raise ConfigError(f"repositories.{repo_id}.execution_profiles contains duplicates")
    for name in profiles:
        if not isinstance(name, str) or name not in _BUILTIN_EXECUTION_PROFILES:
            raise ConfigError(
                f"repositories.{repo_id}.execution_profiles names an unknown profile: "
                f"{name!r}. Reviewed profiles: {', '.join(available_execution_profiles())}"
            )
    return profiles


def resolve_execution_profile_runners(profiles: tuple[str, ...]) -> tuple[str, ...]:
    """The union of runner basenames every enrolled profile contributes, in catalog then
    within-profile order, deduplicated. Silently ignores an unrecognized name -- callers
    that need to reject one should validate_execution_profiles first; this function is
    also used to project an already-validated, persisted value."""
    seen: dict[str, None] = {}
    for name in profiles:
        for runner in _BUILTIN_EXECUTION_PROFILES.get(name, ()):
            seen[runner] = None
    return tuple(seen)


__all__ = [
    "MAX_EXECUTION_PROFILES",
    "available_execution_profiles",
    "resolve_execution_profile_runners",
    "validate_execution_profiles",
]
