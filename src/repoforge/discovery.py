"""Repository detection and configuration rendering for the CLI setup flow."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import tomli as tomllib

from .security import slugify


@dataclass(frozen=True)
class DetectedProfile:
    name: str
    description: str
    verification: bool
    commands: tuple[tuple[str, ...], ...]


@dataclass(frozen=True)
class RepositoryDetection:
    path: Path
    repo_id: str
    display_name: str
    remote: str
    default_base: str
    ecosystem: str
    package_manager: str | None
    package_manager_version: str | None
    scripts: tuple[str, ...]
    instruction_files: tuple[str, ...]
    profiles: tuple[DetectedProfile, ...]
    warnings: tuple[str, ...]


def _run_git(path: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=path,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _detect_package_manager(path: Path, package: dict[str, Any]) -> tuple[str, str | None]:
    declared = package.get("packageManager")
    if isinstance(declared, str) and "@" in declared:
        name, version = declared.split("@", 1)
        if name in {"pnpm", "npm", "yarn", "bun"}:
            return name, version or None
    if (path / "pnpm-lock.yaml").exists():
        return "pnpm", None
    if (path / "bun.lock").exists() or (path / "bun.lockb").exists():
        return "bun", None
    if (path / "yarn.lock").exists():
        return "yarn", None
    return "npm", None


def _script_command(manager: str, script: str) -> tuple[str, ...]:
    if manager == "bun":
        return ("bun", "run", script)
    return (manager, "run", script)


def _js_profiles(manager: str, scripts: set[str]) -> list[DetectedProfile]:
    profiles: list[DetectedProfile] = []
    install_command: tuple[str, ...]
    if manager == "pnpm":
        install_command = ("pnpm", "install", "--frozen-lockfile")
    elif manager == "yarn":
        install_command = ("yarn", "install", "--immutable")
    elif manager == "npm":
        install_command = ("npm", "ci")
    else:
        install_command = ("bun", "install", "--frozen-lockfile")
    profiles.append(
        DetectedProfile(
            name="setup",
            description="Install dependencies exactly from the lockfile",
            verification=False,
            commands=(install_command,),
        )
    )

    if "fix" in scripts:
        profiles.append(
            DetectedProfile(
                name="fix",
                description="Run the repository's autofix script",
                verification=False,
                commands=(_script_command(manager, "fix"),),
            )
        )

    quick_scripts: list[str] = []
    if "check" in scripts:
        quick_scripts = ["check"]
    else:
        quick_scripts = [name for name in ("lint", "typecheck") if name in scripts]
    if quick_scripts:
        profiles.append(
            DetectedProfile(
                name="quick",
                description="Fast static checks for iterative development",
                verification=True,
                commands=tuple(_script_command(manager, name) for name in quick_scripts),
            )
        )

    if "test" in scripts:
        profiles.append(
            DetectedProfile(
                name="test",
                description="Run the repository test suite",
                verification=True,
                commands=(_script_command(manager, "test"),),
            )
        )

    if "test:preflight" in scripts:
        profiles.append(
            DetectedProfile(
                name="preflight",
                description="Run repository preflight or architecture checks",
                verification=True,
                commands=(_script_command(manager, "test:preflight"),),
            )
        )

    full_names: list[str] = []
    if "check" in scripts:
        full_names.append("check")
    else:
        full_names.extend(name for name in ("lint", "typecheck") if name in scripts)
    full_names.extend(name for name in ("test", "test:preflight", "build") if name in scripts)
    # Preserve order while removing duplicates.
    full_names = list(dict.fromkeys(full_names))
    if full_names:
        profiles.append(
            DetectedProfile(
                name="full",
                description="Full verification gate before commit and pull request",
                verification=True,
                commands=tuple(_script_command(manager, name) for name in full_names),
            )
        )
    return profiles


def _python_profiles(path: Path) -> list[DetectedProfile]:
    profiles: list[DetectedProfile] = []
    pyproject = (path / "pyproject.toml").read_text(encoding="utf-8", errors="ignore")
    quick: list[tuple[str, ...]] = []
    if "[tool.ruff" in pyproject:
        quick.append(("python", "-m", "ruff", "check", "."))
    if "[tool.mypy" in pyproject:
        quick.append(("python", "-m", "mypy", "."))
    if quick:
        profiles.append(
            DetectedProfile("quick", "Fast Python static checks", True, tuple(quick))
        )
    test_command = ("python", "-m", "pytest", "-q")
    profiles.append(DetectedProfile("test", "Run the Python test suite", True, (test_command,)))
    full = tuple([*quick, test_command])
    profiles.append(DetectedProfile("full", "Full Python verification gate", True, full))
    return profiles


def _instruction_files(path: Path) -> tuple[str, ...]:
    candidates = (
        "AGENTS.md",
        "CLAUDE.md",
        "CONTRIBUTING.md",
        "README.md",
        ".github/copilot-instructions.md",
        ".cursor/rules",
        "docs/anatomy/README.md",
    )
    return tuple(name for name in candidates if (path / name).exists())


def detect_repository(path: str | Path, repo_id: str | None = None) -> RepositoryDetection:
    root = Path(path).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"Repository path does not exist: {root}")
    if _run_git(root, "rev-parse", "--is-inside-work-tree") != "true":
        raise ValueError(f"Path is not a Git working tree: {root}")

    detected_id = repo_id or slugify(root.name)
    remote = "origin"
    remotes = (_run_git(root, "remote") or "").splitlines()
    if "origin" not in remotes and remotes:
        remote = remotes[0]

    default_base = "main"
    remote_head = _run_git(root, "symbolic-ref", "--short", f"refs/remotes/{remote}/HEAD")
    if remote_head and "/" in remote_head:
        default_base = remote_head.split("/", 1)[1]
    else:
        current = _run_git(root, "branch", "--show-current")
        if current:
            default_base = current

    warnings: list[str] = []
    package_manager: str | None = None
    package_manager_version: str | None = None
    scripts: tuple[str, ...] = ()
    profiles: list[DetectedProfile]
    ecosystem: str

    package = _load_json(root / "package.json")
    if package is not None:
        ecosystem = "javascript"
        package_manager, package_manager_version = _detect_package_manager(root, package)
        raw_scripts = package.get("scripts", {})
        scripts = tuple(sorted(raw_scripts)) if isinstance(raw_scripts, dict) else ()
        profiles = _js_profiles(package_manager, set(scripts))
        if not profiles:
            warnings.append("No supported package.json scripts were detected; add profiles manually.")
    elif (root / "pyproject.toml").exists():
        ecosystem = "python"
        package_manager = "python"
        profiles = _python_profiles(root)
    elif (root / "Cargo.toml").exists():
        ecosystem = "rust"
        package_manager = "cargo"
        profiles = [
            DetectedProfile(
                "quick",
                "Rust formatting and lint checks",
                True,
                (("cargo", "fmt", "--check"), ("cargo", "clippy", "--all-targets", "--", "-D", "warnings")),
            ),
            DetectedProfile("test", "Run Rust tests", True, (("cargo", "test"),)),
            DetectedProfile(
                "full",
                "Full Rust verification gate",
                True,
                (("cargo", "fmt", "--check"), ("cargo", "clippy", "--all-targets", "--", "-D", "warnings"), ("cargo", "test")),
            ),
        ]
    elif (root / "go.mod").exists():
        ecosystem = "go"
        package_manager = "go"
        profiles = [
            DetectedProfile("quick", "Run Go vet", True, (("go", "vet", "./..."),)),
            DetectedProfile("test", "Run Go tests", True, (("go", "test", "./..."),)),
            DetectedProfile(
                "full", "Full Go verification gate", True, (("go", "vet", "./..."), ("go", "test", "./..."))
            ),
        ]
    else:
        ecosystem = "generic"
        profiles = []
        warnings.append("No supported project manifest was detected; configure verification manually.")

    return RepositoryDetection(
        path=root,
        repo_id=detected_id,
        display_name=root.name,
        remote=remote,
        default_base=default_base,
        ecosystem=ecosystem,
        package_manager=package_manager,
        package_manager_version=package_manager_version,
        scripts=scripts,
        instruction_files=_instruction_files(root),
        profiles=tuple(profiles),
        warnings=tuple(warnings),
    )


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _toml_array(values: tuple[str, ...] | list[str]) -> str:
    return "[" + ", ".join(_toml_string(value) for value in values) + "]"


def _toml_commands(commands: tuple[tuple[str, ...], ...]) -> str:
    if len(commands) == 1:
        return _toml_array(list(commands[0]))
    return "[" + ", ".join(_toml_array(list(command)) for command in commands) + "]"


def render_config(detection: RepositoryDetection) -> str:
    verification_profiles = [profile.name for profile in detection.profiles if profile.verification]
    default_verification = "full" if "full" in verification_profiles else (
        verification_profiles[0] if len(verification_profiles) == 1 else None
    )
    lines = [
        "# Generated by RepoForge. Commands are explicit allowlists; review before first use.",
        f"[repositories.{detection.repo_id}]",
        f"path = {_toml_string(str(detection.path))}",
    ]
    if detection.display_name != detection.repo_id:
        lines.append(f"display_name = {_toml_string(detection.display_name)}")
    if detection.remote != "origin":
        lines.append(f"remote = {_toml_string(detection.remote)}")
    if detection.default_base != "main":
        lines.extend(
            [
                f"default_base = {_toml_string(detection.default_base)}",
                f"allowed_base_branches = {_toml_array([detection.default_base])}",
            ]
        )
    if default_verification and default_verification != "full":
        lines.append(f"default_verification_profile = {_toml_string(default_verification)}")

    actions = [profile for profile in detection.profiles if not profile.verification]
    checks = [profile for profile in detection.profiles if profile.verification]
    if actions:
        lines.extend(["", f"[repositories.{detection.repo_id}.actions]"])
        for profile in actions:
            lines.append(f"{profile.name} = {_toml_commands(profile.commands)}")
    if checks:
        lines.extend(["", f"[repositories.{detection.repo_id}.checks]"])
        for profile in checks:
            lines.append(f"{profile.name} = {_toml_commands(profile.commands)}")
    return "\n".join(lines).rstrip() + "\n"


def merge_config(existing: str, detection: RepositoryDetection) -> str:
    try:
        raw = tomllib.loads(existing)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"Cannot update invalid TOML configuration: {exc}") from exc

    repositories = raw.get("repositories", {})
    if not isinstance(repositories, dict):
        raise ValueError("Cannot update configuration: repositories must be a TOML table")
    if detection.repo_id in repositories:
        raise ValueError(
            f"Repository id {detection.repo_id!r} is already configured. "
            "Choose another --repo-id or use --force to replace the entire config."
        )

    current = existing.rstrip()
    generated = render_config(detection).lstrip()
    if not current:
        return generated
    return f"{current}\n\n{generated}"
