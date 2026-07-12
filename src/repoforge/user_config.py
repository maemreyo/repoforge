"""Minimal user configuration and deterministic reviewed runtime locks."""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import tomli as tomllib

from .config import DEFAULT_STATE_ROOT
from .discovery import DetectedProfile, RepositoryDetection, detect_repository, render_config
from .errors import ConfigError
from .security import slugify

_MINIMAL_CONFIG_VERSION = 1
_LOCK_FORMAT_VERSION = 1
_SAFE_ID = re.compile(r"^[A-Za-z0-9._-]+$")
_SAFE_TUNNEL_ID = re.compile(r"^[A-Za-z0-9._:-]+$")
_RELEVANT_PROFILE_FILES = (
    "Makefile",
    "package.json",
    "pnpm-lock.yaml",
    "package-lock.json",
    "yarn.lock",
    "bun.lock",
    "bun.lockb",
    "pyproject.toml",
    "uv.lock",
    "Cargo.toml",
    "Cargo.lock",
    "go.mod",
    "go.sum",
)


@dataclass(frozen=True)
class UserRepository:
    repo_id: str
    path: Path


@dataclass(frozen=True)
class TunnelSettings:
    tunnel_id: str
    profile: str = "repoforge"


@dataclass(frozen=True)
class UserConfig:
    source_path: Path
    tunnel: TunnelSettings
    repositories: tuple[UserRepository, ...]


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _expand_path(value: str, *, base_dir: Path) -> Path:
    expanded = os.path.expandvars(os.path.expanduser(value))
    candidate = Path(expanded)
    if not candidate.is_absolute():
        candidate = base_dir / candidate
    return candidate.resolve()


def read_toml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ConfigError(f"Configuration file not found: {path}")
    try:
        with path.open("rb") as handle:
            value = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"Cannot load configuration {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ConfigError(f"Configuration {path} must contain a TOML table")
    return value


def config_kind(raw: dict[str, Any]) -> str:
    has_minimal = "repo" in raw or "tunnel" in raw
    has_legacy = "repositories" in raw
    if has_minimal and has_legacy:
        raise ConfigError("Do not mix minimal `[[repo]]` and legacy `[repositories.*]` formats")
    return "minimal" if has_minimal else "legacy"


def is_minimal_config(path: str | Path) -> bool:
    config_path = Path(path).expanduser().resolve()
    return config_kind(read_toml(config_path)) == "minimal"


def load_user_config(path: str | Path) -> UserConfig:
    config_path = Path(path).expanduser().resolve()
    raw = read_toml(config_path)
    if config_kind(raw) != "minimal":
        raise ConfigError(
            "This command requires the minimal config format. Run `rf setup ...` to create it."
        )

    version = raw.get("version")
    if (
        not isinstance(version, int)
        or isinstance(version, bool)
        or version != _MINIMAL_CONFIG_VERSION
    ):
        raise ConfigError(
            f"Unsupported minimal config version {version!r}; expected {_MINIMAL_CONFIG_VERSION}"
        )

    tunnel_raw = raw.get("tunnel")
    if not isinstance(tunnel_raw, dict):
        raise ConfigError("[tunnel] is required")
    tunnel_id = tunnel_raw.get("id")
    profile = tunnel_raw.get("profile", "repoforge")
    if not isinstance(tunnel_id, str) or not _SAFE_TUNNEL_ID.fullmatch(tunnel_id):
        raise ConfigError("tunnel.id must be a non-empty safe tunnel identifier")
    if not isinstance(profile, str) or not _SAFE_ID.fullmatch(profile):
        raise ConfigError("tunnel.profile must contain only letters, numbers, '.', '_' or '-'")

    repos_raw = raw.get("repo")
    if not isinstance(repos_raw, list) or not repos_raw:
        raise ConfigError("At least one [[repo]] entry is required")

    repositories: list[UserRepository] = []
    seen_ids: set[str] = set()
    seen_paths: set[Path] = set()
    for index, item in enumerate(repos_raw):
        if not isinstance(item, dict):
            raise ConfigError(f"repos[{index}] must be a TOML table")
        path_raw = item.get("path")
        if not isinstance(path_raw, str) or not path_raw.strip():
            raise ConfigError(f"repos[{index}].path is required")
        repo_path = _expand_path(path_raw, base_dir=config_path.parent)
        repo_id_raw = item.get("id")
        if repo_id_raw is not None and not isinstance(repo_id_raw, str):
            raise ConfigError(f"repos[{index}].id must be a string")
        repo_id = slugify(repo_path.name) if repo_id_raw is None else repo_id_raw
        if not _SAFE_ID.fullmatch(repo_id):
            raise ConfigError(f"Unsafe repository id: {repo_id!r}")
        if repo_id in seen_ids:
            raise ConfigError(f"Duplicate repository id: {repo_id}")
        if repo_path in seen_paths:
            raise ConfigError(f"Duplicate repository path: {repo_path}")
        seen_ids.add(repo_id)
        seen_paths.add(repo_path)
        repositories.append(UserRepository(repo_id=repo_id, path=repo_path))

    return UserConfig(
        source_path=config_path,
        tunnel=TunnelSettings(tunnel_id=tunnel_id, profile=profile),
        repositories=tuple(repositories),
    )


def _validate_user_config(config: UserConfig) -> None:
    if not _SAFE_TUNNEL_ID.fullmatch(config.tunnel.tunnel_id):
        raise ConfigError("tunnel.id must be a non-empty safe tunnel identifier")
    if not _SAFE_ID.fullmatch(config.tunnel.profile):
        raise ConfigError("tunnel.profile must contain only letters, numbers, '.', '_' or '-'")
    if not config.repositories:
        raise ConfigError("At least one repository is required")

    seen_ids: set[str] = set()
    seen_paths: set[Path] = set()
    for repository in config.repositories:
        if not _SAFE_ID.fullmatch(repository.repo_id):
            raise ConfigError(f"Unsafe repository id: {repository.repo_id!r}")
        resolved = repository.path.expanduser().resolve()
        if repository.repo_id in seen_ids:
            raise ConfigError(f"Duplicate repository id: {repository.repo_id}")
        if resolved in seen_paths:
            raise ConfigError(f"Duplicate repository path: {resolved}")
        seen_ids.add(repository.repo_id)
        seen_paths.add(resolved)


def render_user_config(config: UserConfig) -> str:
    _validate_user_config(config)
    lines = [
        "# RepoForge user configuration. Runtime policy is generated into a reviewed lock file.",
        f"version = {_MINIMAL_CONFIG_VERSION}",
        "",
        "[tunnel]",
        f"id = {_toml_string(config.tunnel.tunnel_id)}",
    ]
    if config.tunnel.profile != "repoforge":
        lines.append(f"profile = {_toml_string(config.tunnel.profile)}")
    for repository in config.repositories:
        lines.extend(
            [
                "",
                "[[repo]]",
                f"id = {_toml_string(repository.repo_id)}",
                f"path = {_toml_string(str(repository.path))}",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def resolved_config_path(config_path: str | Path) -> Path:
    source = Path(config_path).expanduser().resolve()
    digest = hashlib.sha256(str(source).encode("utf-8")).hexdigest()[:16]
    return Path(DEFAULT_STATE_ROOT).expanduser().resolve() / "config-locks" / digest / "resolved.toml"


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _make_targets(path: Path) -> set[str]:
    makefile = path / "Makefile"
    if not makefile.is_file() or makefile.is_symlink() or makefile.stat().st_size > 2_000_000:
        return set()
    targets: set[str] = set()
    for line in makefile.read_text(encoding="utf-8", errors="ignore").splitlines():
        if not line or line[0].isspace() or line.startswith("#"):
            continue
        head, separator, _ = line.partition(":")
        if not separator or "=" in head or "%" in head:
            continue
        for target in head.split():
            if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", target):
                targets.add(target)
    return targets


def _make_profiles(path: Path) -> tuple[DetectedProfile, ...]:
    targets = _make_targets(path)
    if not targets:
        return ()
    profiles: list[DetectedProfile] = []

    setup_target = next((name for name in ("bootstrap", "setup", "install") if name in targets), None)
    if setup_target:
        profiles.append(
            DetectedProfile(
                "setup",
                f"Run the repository's `{setup_target}` setup target",
                False,
                (("make", setup_target),),
            )
        )
    if "fix" in targets:
        profiles.append(
            DetectedProfile("fix", "Run the repository's safe autofix target", False, (("make", "fix"),))
        )

    quick_commands = [("make", name) for name in ("lint", "typecheck") if name in targets]
    if not quick_commands and "check" in targets:
        quick_commands = [("make", "check")]
    if quick_commands:
        profiles.append(
            DetectedProfile(
                "quick",
                "Run fast repository checks",
                True,
                tuple(quick_commands),
            )
        )
    if "test" in targets:
        profiles.append(
            DetectedProfile("test", "Run the repository test suite", True, (("make", "test"),))
        )
    if "build" in targets:
        profiles.append(
            DetectedProfile("build", "Build distributable artifacts", True, (("make", "build"),))
        )

    for target in sorted(targets):
        if not target.startswith("check-"):
            continue
        profile_name = target.removeprefix("check-")
        if not profile_name or profile_name in {profile.name for profile in profiles}:
            continue
        profiles.append(
            DetectedProfile(
                profile_name,
                f"Run the `{target}` repository check",
                True,
                (("make", target),),
            )
        )

    full_commands: tuple[tuple[str, ...], ...]
    if "verify" in targets:
        full_commands = (("make", "verify"),)
    elif "check" in targets:
        full_commands = (("make", "check"),)
    else:
        selected = [
            command
            for name in ("lint", "typecheck", "test", "build")
            if name in targets
            for command in (("make", name),)
        ]
        full_commands = tuple(dict.fromkeys(selected))
    if full_commands:
        profiles = [profile for profile in profiles if profile.name != "full"]
        profiles.append(
            DetectedProfile(
                "full",
                "Run the full repository verification gate",
                True,
                full_commands,
            )
        )
    return tuple(profiles)


def detect_repository_for_setup(path: str | Path, repo_id: str | None = None) -> RepositoryDetection:
    detection = detect_repository(path, repo_id)
    make_profiles = _make_profiles(detection.path)
    if any(profile.verification for profile in make_profiles):
        detection = replace(
            detection,
            package_manager="make",
            package_manager_version=None,
            profiles=make_profiles,
        )
    return detection


def _repository_fingerprint(detection: RepositoryDetection) -> str:
    digest = hashlib.sha256()
    metadata = {
        "repo_id": detection.repo_id,
        "path": str(detection.path),
        "remote": detection.remote,
        "default_base": detection.default_base,
        "ecosystem": detection.ecosystem,
        "package_manager": detection.package_manager,
        "package_manager_version": detection.package_manager_version,
        "profiles": [
            {
                "name": profile.name,
                "verification": profile.verification,
                "commands": [list(command) for command in profile.commands],
            }
            for profile in detection.profiles
        ],
    }
    digest.update(json.dumps(metadata, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    for relative in _RELEVANT_PROFILE_FILES:
        candidate = detection.path / relative
        digest.update(b"\x00" + relative.encode("utf-8") + b"\x00")
        if candidate.is_file() and not candidate.is_symlink():
            digest.update(sha256_file(candidate).encode("ascii"))
        else:
            digest.update(b"<missing>")
    return digest.hexdigest()


def _repository_fragment(detection: RepositoryDetection) -> str:
    rendered = render_config(detection)
    marker = f"[repositories.{detection.repo_id}]"
    index = rendered.find(marker)
    if index < 0:
        raise ConfigError(f"Generated config omitted repository {detection.repo_id}")
    return rendered[index:].strip() + "\n"


def _server_fragment(detection: RepositoryDetection) -> str:
    rendered = render_config(detection)
    marker = f"[repositories.{detection.repo_id}]"
    index = rendered.find(marker)
    if index < 0:
        raise ConfigError(f"Generated config omitted repository {detection.repo_id}")
    return rendered[:index].strip() + "\n"


def build_lock_text(config: UserConfig, source_text: str) -> tuple[str, list[RepositoryDetection]]:
    detections = [
        detect_repository_for_setup(repository.path, repository.repo_id)
        for repository in config.repositories
    ]
    fingerprints = {
        detection.repo_id: _repository_fingerprint(detection) for detection in detections
    }
    lines = [
        "# Generated by RepoForge. Do not edit; refresh with `rf repo refresh --accept`.",
        "[repoforge_lock]",
        f"format_version = {_LOCK_FORMAT_VERSION}",
        f"source_config = {_toml_string(str(config.source_path))}",
        f"source_sha256 = {_toml_string(sha256_text(source_text))}",
        "",
        "[repoforge_lock.repositories]",
    ]
    for repo_id in sorted(fingerprints):
        lines.append(f"{_toml_string(repo_id)} = {_toml_string(fingerprints[repo_id])}")
    lines.extend(["", _server_fragment(detections[0]).rstrip(), ""])
    for detection in detections:
        lines.extend([_repository_fragment(detection).rstrip(), ""])
    return "\n".join(lines).rstrip() + "\n", detections


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        temporary.write_text(content, encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def write_user_and_lock(
    config: UserConfig, *, expected_source_sha256: str | None = None
) -> tuple[Path, list[RepositoryDetection]]:
    if expected_source_sha256 is not None:
        if not config.source_path.is_file():
            raise ConfigError(f"Configuration disappeared before update: {config.source_path}")
        actual = sha256_file(config.source_path)
        if actual != expected_source_sha256:
            raise ConfigError("Configuration changed concurrently; reload it and retry")
    source_text = render_user_config(config)
    lock_text, detections = build_lock_text(config, source_text)
    atomic_write(config.source_path, source_text)
    lock_path = resolved_config_path(config.source_path)
    atomic_write(lock_path, lock_text)
    return lock_path, detections


def resolve_runtime_config_path(path: str | Path) -> Path:
    config_path = Path(path).expanduser().resolve()
    raw = read_toml(config_path)
    if config_kind(raw) == "legacy":
        return config_path

    user_config = load_user_config(config_path)
    source_text = config_path.read_text(encoding="utf-8")
    lock_path = resolved_config_path(config_path)
    if not lock_path.is_file():
        raise ConfigError(
            f"Resolved config is missing: {lock_path}. Run `rf repo refresh --accept`."
        )
    expected_lock, _ = build_lock_text(user_config, source_text)
    try:
        actual_lock = lock_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(f"Cannot read resolved config {lock_path}: {exc}") from exc
    if actual_lock != expected_lock:
        raise ConfigError(
            "Resolved config is stale or was modified. Review with `rf repo refresh`, "
            "then accept with `rf repo refresh --accept`."
        )
    return lock_path


def profile_summary(detections: list[RepositoryDetection]) -> dict[str, Any]:
    return {
        detection.repo_id: {
            profile.name: {
                "verification": profile.verification,
                "commands": [list(command) for command in profile.commands],
            }
            for profile in detection.profiles
        }
        for detection in detections
    }
