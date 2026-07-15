"""Bounded native identity discovery helpers."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from pathlib import Path

from ...domain.errors import CommandError
from ...domain.execution_environment import (
    EnvironmentIdentityRequest,
    ToolVersion,
    normalize_tool_name,
)
from ...ports.command import CommandExecutor

_VERSION_SAFE_TOOLS = frozenset(
    {
        "cargo",
        "clang",
        "cmake",
        "gcc",
        "gh",
        "git",
        "go",
        "make",
        "mypy",
        "node",
        "npm",
        "pip",
        "pnpm",
        "python",
        "python3",
        "ruff",
        "rustc",
        "uv",
    }
)


def collect_file_digests(
    workspace_root: Path, names: tuple[str, ...]
) -> tuple[tuple[str, str], ...]:
    """Hash reviewed root-level identity inputs that exist as regular files."""
    digests: list[tuple[str, str]] = []
    for name in names:
        path = workspace_root / name
        if path.is_file() and not path.is_symlink():
            digests.append((name, hashlib.sha256(path.read_bytes()).hexdigest()))
    return tuple(digests)


def collect_environment_hashes(environment: Mapping[str, str]) -> tuple[tuple[str, str], ...]:
    """Hash allowlisted values without exposing environment bodies or user paths."""
    return tuple(
        (name, hashlib.sha256(value.encode("utf-8")).hexdigest())
        for name, value in sorted(environment.items())
    )


def resolve_tools(
    executor: CommandExecutor, request: EnvironmentIdentityRequest
) -> tuple[ToolVersion, ...]:
    """Inspect only executables referenced by the reviewed profile."""
    versions: list[ToolVersion] = []
    for executable in request.tools:
        name = normalize_tool_name(Path(executable).name)
        if name not in _VERSION_SAFE_TOOLS:
            versions.append(ToolVersion(name=name))
            continue
        try:
            result = executor.run(
                (executable, "--version"),
                cwd=request.command_cwd,
                timeout=5,
                check=False,
                output_limit=256,
            )
        except CommandError:
            versions.append(ToolVersion(name=name))
            continue
        raw = (result.stdout or result.stderr).strip()
        version = raw.splitlines()[0][:128] if result.returncode == 0 and raw else None
        versions.append(ToolVersion(name=name, version=version))
    return tuple(versions)
