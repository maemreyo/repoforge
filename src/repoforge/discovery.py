"""Repository detection, bounded local scanning, and configuration rendering."""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterable

from .config import DEFAULT_DENIED_PATHS, DEFAULT_PATH_PREFIXES
from .security import slugify

DEFAULT_SCAN_EXCLUDES = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        "node_modules",
        "vendor",
        "dist",
        "build",
        "target",
        "coverage",
        ".cache",
        ".pytest_cache",
        ".ruff_cache",
        ".mypy_cache",
        "__pycache__",
    }
)
_MAX_SCAN_DEPTH = 10
_MAX_SCAN_REPOSITORIES = 500
_MAKE_TARGET = re.compile(r"^([A-Za-z0-9_.-]+)\s*:(?![=])")


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

    quick_scripts: list[str]
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


def _make_targets(path: Path) -> set[str]:
    makefile = path / "Makefile"
    if not makefile.is_file():
        return set()
    try:
        lines = makefile.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return set()
    targets: set[str] = set()
    for line in lines:
        if line.startswith((" ", "\t", ".")):
            continue
        match = _MAKE_TARGET.match(line)
        if match:
            targets.add(match.group(1))
    return targets


def _make_profiles(targets: set[str]) -> list[DetectedProfile]:
    profiles: list[DetectedProfile] = []
    setup_target = "setup" if "setup" in targets else "bootstrap" if "bootstrap" in targets else None
    if setup_target:
        profiles.append(
            DetectedProfile(
                "setup",
                "Install or synchronize repository dependencies",
                False,
                (("make", setup_target),),
            )
        )
    if "fix" in targets:
        profiles.append(
            DetectedProfile("fix", "Apply the repository's safe automatic fixes", False, (("make", "fix"),))
        )

    quick_targets = [name for name in ("lint", "typecheck") if name in targets]
    if not quick_targets:
        quick_targets = [name for name in ("check-static", "check") if name in targets][:1]
    if quick_targets:
        profiles.append(
            DetectedProfile(
                "quick",
                "Fast repository checks for iterative development",
                True,
                tuple(("make", name) for name in quick_targets),
            )
        )

    specialized = (
        ("test", "Run the repository test suite", "test"),
        ("preflight", "Run repository preflight checks", "check-preflight"),
        ("architecture", "Run repository architecture checks", "check-architecture"),
        ("contracts", "Check generated or executable contracts", "check-contracts"),
        ("registry", "Check generated registry artifacts", "check-harness-registry"),
        ("build", "Build distributable repository artifacts", "build"),
        ("recertify", "Run repository recertification", "recertify-foundation"),
    )
    for profile_name, description, target in specialized:
        if target in targets:
            profiles.append(
                DetectedProfile(profile_name, description, True, (("make", target),))
            )

    if "verify" in targets:
        full_commands = (("make", "verify"),)
    elif "check" in targets:
        full_commands = (("make", "check"),)
    else:
        full_commands = tuple(
            dict.fromkeys(
                command
                for profile in profiles
                if profile.verification and profile.name not in {"recertify", "full"}
                for command in profile.commands
            )
        )
    if full_commands:
        profiles.append(
            DetectedProfile(
                "full",
                "Full repository verification gate before commit and pull request",
                True,
                full_commands,
            )
        )
    return profiles


def _python_profiles(path: Path) -> list[DetectedProfile]:
    make_profiles = _make_profiles(_make_targets(path))
    if make_profiles:
        return make_profiles

    profiles: list[DetectedProfile] = []
    pyproject = (path / "pyproject.toml").read_text(encoding="utf-8", errors="ignore")
    uses_uv = (path / "uv.lock").is_file()
    runner = ("uv", "run") if uses_uv else ("python", "-m")
    if uses_uv:
        setup = ("uv", "sync", "--extra", "dev") if "[project.optional-dependencies]" in pyproject else ("uv", "sync")
        profiles.append(DetectedProfile("setup", "Synchronize the locked Python environment", False, (setup,)))

    quick: list[tuple[str, ...]] = []
    if "[tool.ruff" in pyproject:
        quick.append((*runner, "ruff", "check", "."))
    if "[tool.mypy" in pyproject:
        quick.append((*runner, "mypy", "."))
    if quick:
        profiles.append(DetectedProfile("quick", "Fast Python static checks", True, tuple(quick)))

    test_command = (*runner, "pytest", "-q")
    profiles.append(DetectedProfile("test", "Run the Python test suite", True, (test_command,)))
    build_commands: tuple[tuple[str, ...], ...] = ()
    if "[build-system]" in pyproject and uses_uv:
        build_commands = (("uv", "build"),)
        profiles.append(DetectedProfile("build", "Build wheel and source distribution", True, build_commands))
    full = tuple([*quick, test_command, *build_commands])
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
    if not remotes:
        warnings.append("No Git remote was detected; configure `remote` before push or PR operations.")
    if _run_git(root, "status", "--porcelain"):
        warnings.append("Working tree has uncommitted changes; review them before creating a workspace.")

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
        make_profiles = _make_profiles(_make_targets(root))
        profiles = make_profiles or _js_profiles(package_manager, set(scripts))
        if not profiles:
            warnings.append("No supported package.json scripts were detected; add profiles manually.")
    elif (root / "pyproject.toml").exists():
        ecosystem = "python"
        package_manager = "uv" if (root / "uv.lock").exists() else "python"
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
        profiles = _make_profiles(_make_targets(root))
        if not profiles:
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


def scan_repository_paths(
    roots: Iterable[str | Path],
    *,
    max_depth: int = 3,
    max_repositories: int = 100,
    include_hidden: bool = False,
) -> tuple[Path, ...]:
    """Find top-level Git repositories under explicit roots without following symlinks."""
    if not 0 <= max_depth <= _MAX_SCAN_DEPTH:
        raise ValueError(f"max_depth must be between 0 and {_MAX_SCAN_DEPTH}")
    if not 1 <= max_repositories <= _MAX_SCAN_REPOSITORIES:
        raise ValueError(f"max_repositories must be between 1 and {_MAX_SCAN_REPOSITORIES}")

    queue: list[tuple[Path, int]] = []
    for root_value in roots:
        root = Path(root_value).expanduser().resolve()
        if not root.is_dir():
            raise ValueError(f"Scan root does not exist or is not a directory: {root}")
        queue.append((root, 0))
    if not queue:
        raise ValueError("At least one scan root is required")

    found: list[Path] = []
    visited: set[Path] = set()
    while queue:
        current, depth = queue.pop(0)
        if current in visited:
            continue
        visited.add(current)

        git_marker = current / ".git"
        if git_marker.exists() and _run_git(current, "rev-parse", "--show-toplevel"):
            found.append(current)
            if len(found) >= max_repositories:
                break
            continue
        if depth >= max_depth:
            continue

        try:
            entries = sorted(os.scandir(current), key=lambda entry: entry.name.casefold())
        except OSError:
            continue
        for entry in entries:
            name = entry.name
            if name in DEFAULT_SCAN_EXCLUDES:
                continue
            if not include_hidden and name.startswith("."):
                continue
            try:
                if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                    continue
            except OSError:
                continue
            queue.append((Path(entry.path).resolve(), depth + 1))

    return tuple(sorted(set(found), key=lambda path: str(path).casefold()))


def _unique_repo_ids(detections: Iterable[RepositoryDetection]) -> tuple[RepositoryDetection, ...]:
    used: set[str] = set()
    result: list[RepositoryDetection] = []
    for detection in sorted(detections, key=lambda item: str(item.path).casefold()):
        base = detection.repo_id
        candidate = base
        suffix = 2
        while candidate in used:
            candidate = f"{base}-{suffix}"
            suffix += 1
        used.add(candidate)
        result.append(detection if candidate == base else replace(detection, repo_id=candidate))
    return tuple(result)


def scan_repositories(
    roots: Iterable[str | Path],
    *,
    max_depth: int = 3,
    max_repositories: int = 100,
    include_hidden: bool = False,
) -> tuple[RepositoryDetection, ...]:
    paths = scan_repository_paths(
        roots,
        max_depth=max_depth,
        max_repositories=max_repositories,
        include_hidden=include_hidden,
    )
    return _unique_repo_ids(detect_repository(path) for path in paths)


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _toml_array(values: tuple[str, ...] | list[str]) -> str:
    return "[" + ", ".join(_toml_string(value) for value in values) + "]"


def _render_repository(detection: RepositoryDetection) -> list[str]:
    verification_profiles = [profile.name for profile in detection.profiles if profile.verification]
    default_verification = (
        "full"
        if "full" in verification_profiles
        else verification_profiles[0]
        if verification_profiles
        else None
    )
    lines = [
        f"[repositories.{detection.repo_id}]",
        f"path = {_toml_string(str(detection.path))}",
        f"display_name = {_toml_string(detection.display_name)}",
        f"remote = {_toml_string(detection.remote)}",
        f"default_base = {_toml_string(detection.default_base)}",
        f"allowed_base_branches = {_toml_array([detection.default_base])}",
        'branch_prefix = "ai/"',
        'protected_branches = ["main", "master", "develop", "production"]',
        "require_verification_before_commit = true",
        "fetch_before_workspace = true",
    ]
    if default_verification:
        lines.append(f"default_verification_profile = {_toml_string(default_verification)}")
    lines.extend(
        [
            "max_changed_files = 150",
            "max_diff_lines = 12000",
            "max_total_changed_bytes = 26214400",
            "allowed_paths = []",
            f"denied_paths = {_toml_array(list(DEFAULT_DENIED_PATHS))}",
            "pr_labels = []",
            "pr_reviewers = []",
            "no_maintainer_edit = false",
            "",
        ]
    )
    for profile in detection.profiles:
        lines.extend(
            [
                f"[repositories.{detection.repo_id}.profiles.{profile.name}]",
                f"description = {_toml_string(profile.description)}",
                f"verification = {'true' if profile.verification else 'false'}",
                "commands = [",
            ]
        )
        for command in profile.commands:
            lines.append(f"  {_toml_array(list(command))},")
        lines.extend(["]", ""])
    return lines


def render_config_set(detections: Iterable[RepositoryDetection]) -> str:
    normalized = _unique_repo_ids(detections)
    if not normalized:
        raise ValueError("At least one repository detection is required")
    lines = [
        "# Generated by RepoForge. Review every detected command before first use.",
        "[server]",
        'workspace_root = "~/.local/share/repoforge/workspaces"',
        'state_root = "~/.local/state/repoforge"',
        "max_file_bytes = 2000000",
        "max_tool_output_chars = 120000",
        "default_command_timeout_seconds = 120",
        "verification_timeout_seconds = 1800",
        "max_fingerprint_bytes = 52428800",
        "max_batch_files = 20",
        f"path_prefixes = {_toml_array(list(DEFAULT_PATH_PREFIXES))}",
        'allowed_environment = ["HOME", "PATH", "LANG", "LC_ALL", "SSH_AUTH_SOCK", "GH_HOST", "GIT_SSH_COMMAND", "COREPACK_HOME", "PNPM_HOME", "UV_CACHE_DIR", "XDG_CACHE_HOME", "DOCKER_HOST"]',
        "",
    ]
    for detection in normalized:
        lines.extend(_render_repository(detection))
    return "\n".join(lines).rstrip() + "\n"


def render_config(detection: RepositoryDetection) -> str:
    return render_config_set((detection,))
