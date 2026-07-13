"""TOML configuration loading and validation."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import tomli as tomllib

from .domain.errors import ConfigError

DEFAULT_CONFIG_PATH = Path("~/.config/repoforge/config.toml").expanduser()
DEFAULT_WORKSPACE_ROOT = "~/.local/share/repoforge/workspaces"
DEFAULT_STATE_ROOT = "~/.local/state/repoforge"
DEFAULT_PATH_PREFIXES = ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin")
DEFAULT_ALLOWED_ENVIRONMENT = (
    "HOME",
    "PATH",
    "LANG",
    "LC_ALL",
    "SSH_AUTH_SOCK",
    "GH_HOST",
    "GIT_SSH_COMMAND",
    "COREPACK_HOME",
    "PNPM_HOME",
)
DEFAULT_DENIED_PATHS = (
    ".git",
    ".git/**",
    ".env",
    ".env.*",
    "**/.env",
    "**/.env.*",
    "**/*.pem",
    "**/*.key",
    "**/*secret*",
    "**/*credential*",
    ".github/workflows/**",
)
_SAFE_BRANCH_COMPONENT = re.compile(r"^[A-Za-z0-9._/-]+$")
_SAFE_REPO_ID = re.compile(r"^[A-Za-z0-9._-]+$")


@dataclass(frozen=True)
class ProfileConfig:
    name: str
    description: str
    commands: tuple[tuple[str, ...], ...]
    verification: bool = False
    timeout_seconds: int | None = None
    working_directory: str | None = None


@dataclass(frozen=True)
class RepositoryConfig:
    repo_id: str
    path: Path
    display_name: str = ""
    remote: str = "origin"
    default_base: str = "main"
    allowed_base_branches: tuple[str, ...] = ("main",)
    branch_prefix: str = "ai/"
    protected_branches: tuple[str, ...] = ("main", "master", "develop", "production")
    read_only: bool = False
    publish_enabled: bool = True
    require_verification_before_commit: bool = True
    fetch_before_workspace: bool = True
    default_verification_profile: str | None = None
    max_changed_files: int = 150
    max_diff_lines: int = 12_000
    max_total_changed_bytes: int = 25 * 1024 * 1024
    allowed_paths: tuple[str, ...] = ()
    denied_paths: tuple[str, ...] = DEFAULT_DENIED_PATHS
    pr_labels: tuple[str, ...] = ()
    pr_reviewers: tuple[str, ...] = ()
    no_maintainer_edit: bool = False
    profiles: dict[str, ProfileConfig] = field(default_factory=dict)


@dataclass(frozen=True)
class ServerConfig:
    workspace_root: Path
    state_root: Path
    max_file_bytes: int = 2_000_000
    max_tool_output_chars: int = 120_000
    default_command_timeout_seconds: int = 120
    verification_timeout_seconds: int = 1_800
    max_fingerprint_bytes: int = 50 * 1024 * 1024
    max_batch_files: int = 20
    path_prefixes: tuple[str, ...] = DEFAULT_PATH_PREFIXES
    allowed_environment: tuple[str, ...] = DEFAULT_ALLOWED_ENVIRONMENT


@dataclass(frozen=True)
class AppConfig:
    source_path: Path
    server: ServerConfig
    repositories: dict[str, RepositoryConfig]


def _expand_path(value: str, *, base_dir: Path) -> Path:
    expanded = os.path.expandvars(os.path.expanduser(value))
    path = Path(expanded)
    if not path.is_absolute():
        path = base_dir / path
    return path.resolve()


def _expect_mapping(value: Any, context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{context} must be a TOML table")
    return value


def _tuple_of_strings(value: Any, context: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigError(f"{context} must be an array of strings")
    return tuple(value)


def _positive_int(value: Any, default: int, context: str) -> int:
    if value is None:
        return default
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ConfigError(f"{context} must be a positive integer")
    return value


def _boolean(value: Any, default: bool, context: str) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ConfigError(f"{context} must be true or false")
    return value


def _safe_ref(value: str, context: str) -> str:
    if not value or not _SAFE_BRANCH_COMPONENT.fullmatch(value):
        raise ConfigError(f"{context} contains unsafe characters: {value!r}")
    if value.startswith("-") or ".." in value or value.endswith("/"):
        raise ConfigError(f"{context} is not a safe Git ref component: {value!r}")
    return value


def _safe_remote(value: str, context: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9._-]+", value) or value.startswith("-"):
        raise ConfigError(f"{context} must be a safe configured remote name: {value!r}")
    return value


def _load_profiles(raw: Any, repo_id: str) -> dict[str, ProfileConfig]:
    if raw is None:
        return {}
    table = _expect_mapping(raw, f"repositories.{repo_id}.profiles")
    profiles: dict[str, ProfileConfig] = {}
    for name, profile_raw in table.items():
        _safe_ref(name, f"profile name for {repo_id}")
        profile = _expect_mapping(profile_raw, f"repositories.{repo_id}.profiles.{name}")
        description = str(profile.get("description", ""))
        verification = _boolean(
            profile.get("verification"), False, f"profile {repo_id}.{name}.verification"
        )
        working_directory_raw = profile.get("working_directory")
        working_directory = None
        if working_directory_raw is not None:
            if not isinstance(working_directory_raw, str):
                raise ConfigError(f"profile {repo_id}.{name}.working_directory must be a string")
            normalized_workdir = working_directory_raw.replace("\\", "/").strip("/")
            if (
                not normalized_workdir
                or normalized_workdir.startswith("-")
                or any(part in {"", ".", ".."} for part in normalized_workdir.split("/"))
            ):
                raise ConfigError(
                    f"profile {repo_id}.{name}.working_directory must be a safe relative path"
                )
            working_directory = normalized_workdir
        timeout_raw = profile.get("timeout_seconds")
        timeout_seconds = None
        if timeout_raw is not None:
            timeout_seconds = _positive_int(
                timeout_raw, 1, f"profile {repo_id}.{name}.timeout_seconds"
            )
        raw_commands = profile.get("commands")
        if not isinstance(raw_commands, list) or not raw_commands:
            raise ConfigError(f"profile {repo_id}.{name} must contain at least one command")
        commands: list[tuple[str, ...]] = []
        for index, command in enumerate(raw_commands):
            if (
                not isinstance(command, list)
                or not command
                or not all(isinstance(arg, str) and arg for arg in command)
            ):
                raise ConfigError(
                    f"profile {repo_id}.{name}.commands[{index}] must be a non-empty string array"
                )
            commands.append(tuple(command))
        profiles[name] = ProfileConfig(
            name=name,
            description=description,
            commands=tuple(commands),
            verification=verification,
            timeout_seconds=timeout_seconds,
            working_directory=working_directory,
        )
    return profiles


def load_config(path: str | Path | None = None) -> AppConfig:
    config_value: str | Path = path or os.environ.get("REPOFORGE_CONFIG", str(DEFAULT_CONFIG_PATH))
    config_path = Path(config_value).expanduser().resolve()
    if not config_path.is_file():
        raise ConfigError(
            f"Configuration file not found: {config_path}. "
            "Run `repoforge init --repo /path/to/repo` first."
        )
    try:
        with config_path.open("rb") as handle:
            raw = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"Cannot load configuration {config_path}: {exc}") from exc
    base_dir = config_path.parent
    server_raw = _expect_mapping(raw.get("server", {}), "server")
    server = ServerConfig(
        workspace_root=_expand_path(
            str(server_raw.get("workspace_root", DEFAULT_WORKSPACE_ROOT)), base_dir=base_dir
        ),
        state_root=_expand_path(
            str(server_raw.get("state_root", DEFAULT_STATE_ROOT)), base_dir=base_dir
        ),
        max_file_bytes=_positive_int(
            server_raw.get("max_file_bytes"), 2_000_000, "server.max_file_bytes"
        ),
        max_tool_output_chars=_positive_int(
            server_raw.get("max_tool_output_chars"), 120_000, "server.max_tool_output_chars"
        ),
        default_command_timeout_seconds=_positive_int(
            server_raw.get("default_command_timeout_seconds"),
            120,
            "server.default_command_timeout_seconds",
        ),
        verification_timeout_seconds=_positive_int(
            server_raw.get("verification_timeout_seconds"),
            1_800,
            "server.verification_timeout_seconds",
        ),
        max_fingerprint_bytes=_positive_int(
            server_raw.get("max_fingerprint_bytes"),
            50 * 1024 * 1024,
            "server.max_fingerprint_bytes",
        ),
        max_batch_files=_positive_int(
            server_raw.get("max_batch_files"), 20, "server.max_batch_files"
        ),
        path_prefixes=_tuple_of_strings(server_raw.get("path_prefixes"), "server.path_prefixes")
        or DEFAULT_PATH_PREFIXES,
        allowed_environment=_tuple_of_strings(
            server_raw.get("allowed_environment"), "server.allowed_environment"
        )
        or DEFAULT_ALLOWED_ENVIRONMENT,
    )
    repositories_raw = _expect_mapping(raw.get("repositories"), "repositories")
    if not repositories_raw:
        raise ConfigError("At least one repository must be configured")
    repositories: dict[str, RepositoryConfig] = {}
    for repo_id, repo_raw_any in repositories_raw.items():
        if not _SAFE_REPO_ID.fullmatch(repo_id):
            raise ConfigError(f"Unsafe repository id: {repo_id!r}")
        repo_raw = _expect_mapping(repo_raw_any, f"repositories.{repo_id}")
        if "path" not in repo_raw:
            raise ConfigError(f"repositories.{repo_id}.path is required")
        default_base = _safe_ref(
            str(repo_raw.get("default_base", "main")), f"repositories.{repo_id}.default_base"
        )
        allowed_bases = _tuple_of_strings(
            repo_raw.get("allowed_base_branches", [default_base]),
            f"repositories.{repo_id}.allowed_base_branches",
        )
        if not allowed_bases:
            allowed_bases = (default_base,)
        allowed_bases = tuple(
            _safe_ref(item, f"repositories.{repo_id}.allowed_base_branches")
            for item in allowed_bases
        )
        if default_base not in allowed_bases:
            raise ConfigError(
                f"repositories.{repo_id}.default_base must be in allowed_base_branches"
            )
        branch_prefix = str(repo_raw.get("branch_prefix", "ai/"))
        if not branch_prefix.endswith("/") or not _SAFE_BRANCH_COMPONENT.fullmatch(branch_prefix):
            raise ConfigError(
                f"repositories.{repo_id}.branch_prefix must be a safe prefix ending in '/': "
                f"{branch_prefix!r}"
            )
        protected = _tuple_of_strings(
            repo_raw.get("protected_branches", ["main", "master", "develop", "production"]),
            f"repositories.{repo_id}.protected_branches",
        )
        profiles = _load_profiles(repo_raw.get("profiles"), repo_id)
        default_verification_raw = repo_raw.get("default_verification_profile")
        default_verification = (
            str(default_verification_raw) if default_verification_raw is not None else None
        )
        if default_verification:
            if default_verification not in profiles:
                raise ConfigError(
                    f"repositories.{repo_id}.default_verification_profile references unknown "
                    f"profile {default_verification!r}"
                )
            if not profiles[default_verification].verification:
                raise ConfigError(
                    f"repositories.{repo_id}.default_verification_profile must reference a "
                    "verification profile"
                )
        repositories[repo_id] = RepositoryConfig(
            repo_id=repo_id,
            path=_expand_path(str(repo_raw["path"]), base_dir=base_dir),
            display_name=str(repo_raw.get("display_name", repo_id)),
            remote=_safe_remote(
                str(repo_raw.get("remote", "origin")), f"repositories.{repo_id}.remote"
            ),
            default_base=default_base,
            allowed_base_branches=allowed_bases,
            branch_prefix=branch_prefix,
            protected_branches=protected,
            read_only=_boolean(
                repo_raw.get("read_only"),
                False,
                f"repositories.{repo_id}.read_only",
            ),
            publish_enabled=_boolean(
                repo_raw.get("publish_enabled"),
                True,
                f"repositories.{repo_id}.publish_enabled",
            ),
            require_verification_before_commit=_boolean(
                repo_raw.get("require_verification_before_commit"),
                True,
                f"repositories.{repo_id}.require_verification_before_commit",
            ),
            fetch_before_workspace=_boolean(
                repo_raw.get("fetch_before_workspace"),
                True,
                f"repositories.{repo_id}.fetch_before_workspace",
            ),
            default_verification_profile=default_verification,
            max_changed_files=_positive_int(
                repo_raw.get("max_changed_files"), 150, f"repositories.{repo_id}.max_changed_files"
            ),
            max_diff_lines=_positive_int(
                repo_raw.get("max_diff_lines"), 12_000, f"repositories.{repo_id}.max_diff_lines"
            ),
            max_total_changed_bytes=_positive_int(
                repo_raw.get("max_total_changed_bytes"),
                25 * 1024 * 1024,
                f"repositories.{repo_id}.max_total_changed_bytes",
            ),
            allowed_paths=_tuple_of_strings(
                repo_raw.get("allowed_paths", []), f"repositories.{repo_id}.allowed_paths"
            ),
            denied_paths=_tuple_of_strings(
                repo_raw.get("denied_paths", list(DEFAULT_DENIED_PATHS)),
                f"repositories.{repo_id}.denied_paths",
            ),
            pr_labels=_tuple_of_strings(
                repo_raw.get("pr_labels", []), f"repositories.{repo_id}.pr_labels"
            ),
            pr_reviewers=_tuple_of_strings(
                repo_raw.get("pr_reviewers", []), f"repositories.{repo_id}.pr_reviewers"
            ),
            no_maintainer_edit=_boolean(
                repo_raw.get("no_maintainer_edit"),
                False,
                f"repositories.{repo_id}.no_maintainer_edit",
            ),
            profiles=profiles,
        )
    return AppConfig(source_path=config_path, server=server, repositories=repositories)
