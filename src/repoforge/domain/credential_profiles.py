"""Named, opt-in credential-scoping profiles for generic shell/ad-hoc execution (#381).

The server's global `allowed_environment` baseline (HOME/PATH/LANG/LC_ALL plus a small
set of already-established git/gh transport variables) is unconditional: every ad-hoc
command in every repository sees it, with no per-run opt-in. That baseline is left
unchanged here -- it is not this module's job to revoke what already existed.

What is missing is a way to grant *additional* credential-shaped environment variables
-- npm/pnpm registry auth, a Docker daemon socket, cloud CLI config -- to a specific
repository's ad-hoc/exec commands only when that repository explicitly opts in, instead
of the only alternative being to widen the global baseline for every repository at once.
A profile here is exactly that: a named, reviewed set of environment-variable *names*
(never values -- see resolve_credential_profile_env) a repository can enroll in via
`RepositoryConfig.credential_profiles`, resolved per run into whichever of those names
are actually present in the host environment.

The catalog is fixed, not operator-authored, for the same reason execution_profiles'
is: an unknown name is always a typo, never a legitimate custom profile.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from .errors import ConfigError

MAX_CREDENTIAL_PROFILES = 16

_BUILTIN_CREDENTIAL_PROFILES: dict[str, tuple[str, ...]] = {
    "npm_registry": ("NPM_TOKEN", "NPM_CONFIG_REGISTRY"),
    "docker": ("DOCKER_HOST", "DOCKER_CONFIG", "DOCKER_CERT_PATH"),
    "cloud_aws": (
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_PROFILE",
        "AWS_CONFIG_FILE",
        "AWS_SHARED_CREDENTIALS_FILE",
    ),
    "cloud_gcp": ("GOOGLE_APPLICATION_CREDENTIALS", "CLOUDSDK_CONFIG", "CLOUDSDK_CORE_PROJECT"),
    "cloud_azure": (
        "AZURE_CONFIG_DIR",
        "AZURE_CLIENT_ID",
        "AZURE_CLIENT_SECRET",
        "AZURE_TENANT_ID",
    ),
}


def available_credential_profiles() -> tuple[str, ...]:
    """The reviewed profile catalog, in a stable order -- for operator-facing discovery,
    e.g. naming the valid choices in a config error."""
    return tuple(_BUILTIN_CREDENTIAL_PROFILES)


def validate_credential_profiles(profiles: tuple[str, ...], repo_id: str) -> tuple[str, ...]:
    """Validate a repository's enrolled credential-profile names at config-load time."""
    if len(profiles) > MAX_CREDENTIAL_PROFILES:
        raise ConfigError(
            f"repositories.{repo_id}.credential_profiles must not exceed "
            f"{MAX_CREDENTIAL_PROFILES} entries"
        )
    if len(set(profiles)) != len(profiles):
        raise ConfigError(f"repositories.{repo_id}.credential_profiles contains duplicates")
    for name in profiles:
        if not isinstance(name, str) or name not in _BUILTIN_CREDENTIAL_PROFILES:
            raise ConfigError(
                f"repositories.{repo_id}.credential_profiles names an unknown profile: "
                f"{name!r}. Reviewed profiles: {', '.join(available_credential_profiles())}"
            )
    return profiles


def resolve_credential_profile_env_names(profiles: tuple[str, ...]) -> tuple[str, ...]:
    """The union of environment-variable *names* every enrolled profile grants, in
    catalog then within-profile order, deduplicated. Never returns a value -- see
    resolve_credential_profile_env for that. Silently ignores an unrecognized name --
    validate_credential_profiles is what rejects one; this projects an already-validated
    value."""
    seen: dict[str, None] = {}
    for name in profiles:
        for var in _BUILTIN_CREDENTIAL_PROFILES.get(name, ()):
            seen[var] = None
    return tuple(seen)


def resolve_credential_profile_env(
    profiles: tuple[str, ...], *, environ: Mapping[str, str] | None = None
) -> tuple[tuple[str, str], ...]:
    """The actual (name, value) pairs a repository's enrolled profiles grant for one
    run, reading only the names those profiles list and only when the host process
    actually has them set -- the same "allowlist of names, not an exclusion list"
    orientation ServerConfig.allowed_environment already uses, extended to a per-
    repository, opt-in set instead of the unconditional global one.
    """
    source = os.environ if environ is None else environ
    return tuple(
        (name, source[name])
        for name in resolve_credential_profile_env_names(profiles)
        if name in source
    )


__all__ = [
    "MAX_CREDENTIAL_PROFILES",
    "available_credential_profiles",
    "resolve_credential_profile_env",
    "resolve_credential_profile_env_names",
    "validate_credential_profiles",
]
