"""Native reviewed execution adapter — delegates to the constrained subprocess executor."""

from __future__ import annotations

import hashlib
import platform
import shutil
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from repoforge.domain.execution_environment import (
    EnvironmentAdapterKind,
    EnvironmentIdentity,
    FilesystemCapability,
    NetworkPolicy,
    ToolVersion,
    normalize_tool_name,
)
from repoforge.ports.command import CommandExecutor
from repoforge.ports.execution_environment import (
    ArtifactResult,
    ExecutionReceipt,
)


def _resolve_tool_version(tool: str) -> ToolVersion:
    resolved = shutil.which(tool)
    if resolved is None:
        return ToolVersion(name=normalize_tool_name(tool))
    try:
        completed = subprocess.run(
            [tool, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        raw = (completed.stdout or completed.stderr or "").strip()
        if not raw or completed.returncode != 0:
            return ToolVersion(name=normalize_tool_name(tool))
        version = raw.splitlines()[0][:128]
        return ToolVersion(
            name=normalize_tool_name(tool),
            version=version,
        )
    except (OSError, subprocess.SubprocessError):
        return ToolVersion(name=normalize_tool_name(tool))


def _compute_file_digest(path: Path) -> str | None:
    """SHA-256 digest of a file, or None if unavailable."""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _find_lockfiles(project_root: Path) -> tuple[tuple[str, str], ...]:
    """Discover common lockfile digests under project_root."""
    digests: list[tuple[str, str]] = []
    candidates = [
        "uv.lock",
        "poetry.lock",
        "Pipfile.lock",
        "requirements.txt",
        "pnpm-lock.yaml",
        "yarn.lock",
        "package-lock.json",
        "Cargo.lock",
        "go.sum",
        "Gemfile.lock",
    ]
    for name in candidates:
        candidate = project_root / name
        digest = _compute_file_digest(candidate)
        if digest is not None:
            digests.append((name, digest))
    return tuple(digests)


def _find_manifests(project_root: Path) -> tuple[tuple[str, str], ...]:
    """Discover common manifest digests under project_root."""
    digests: list[tuple[str, str]] = []
    candidates = [
        "pyproject.toml",
        "package.json",
        "Cargo.toml",
        "go.mod",
    ]
    for name in candidates:
        candidate = project_root / name
        digest = _compute_file_digest(candidate)
        if digest is not None:
            digests.append((name, digest))
    return tuple(digests)


_REVIEWED_TOOLS: tuple[str, ...] = (
    "python",
    "python3",
    "git",
    "gh",
    "uv",
    "pip",
    "node",
    "npm",
    "cargo",
    "go",
    "rustc",
    "make",
    "cmake",
    "gcc",
    "clang",
    "docker",
)

_DEFAULT_APPROVED_ENV_VARS: tuple[str, ...] = (
    "PATH",
    "HOME",
    "USER",
    "LANG",
    "LC_ALL",
    "GIT_TERMINAL_PROMPT",
    "GH_PROMPT_DISABLED",
)


class NativeReviewedAdapter:
    """Native execution adapter that delegates to the existing constrained executor.

    Preserves current behavior while adding environment fingerprinting for
    identity, cache eligibility, and execution receipts.
    """

    def __init__(
        self,
        executor: CommandExecutor,
        *,
        project_root: Path | None = None,
        approved_tools: tuple[str, ...] = _REVIEWED_TOOLS,
        approved_env_var_names: tuple[str, ...] = _DEFAULT_APPROVED_ENV_VARS,
        network_policy: NetworkPolicy = NetworkPolicy.NONE,
        filesystem_capability: FilesystemCapability = FilesystemCapability.WORKSPACE_WRITE,
    ) -> None:
        self._executor = executor
        self._project_root = project_root
        self._approved_tools = approved_tools
        self._approved_env_var_names = approved_env_var_names
        self._network_policy = network_policy
        self._filesystem_capability = filesystem_capability
        self._cached_identity: EnvironmentIdentity | None = None

    # -- ExecutionEnvironmentPort implementation --

    def doctor(self) -> tuple[str, ...]:
        """Return health warnings (empty = healthy)."""
        warnings: list[str] = []
        for tool in self._approved_tools:
            tv = _resolve_tool_version(tool)
            if tv.version is None:
                warnings.append(f"Reviewed tool '{tool}' is not available in PATH")
        return tuple(warnings)

    def prepare(self, *, cwd: Path, extra_env: Mapping[str, str] | None = None) -> None:
        """Prepare — idempotent no-op for native execution."""
        _ = cwd, extra_env  # Native env is already prepared

    def identity(self) -> EnvironmentIdentity:
        """Fingerprint the native execution environment."""
        if self._cached_identity is not None:
            return self._cached_identity

        tools: list[ToolVersion] = []
        for tool in self._approved_tools:
            tv = _resolve_tool_version(tool)
            tools.append(tv)

        lockfile_digests: tuple[tuple[str, str], ...] = ()
        manifest_digests: tuple[tuple[str, str], ...] = ()
        wd_policy_hash = ""
        if self._project_root is not None:
            lockfile_digests = _find_lockfiles(self._project_root)
            manifest_digests = _find_manifests(self._project_root)
            wd_policy_hash = hashlib.sha256(
                str(self._project_root.resolve()).encode()
            ).hexdigest()

        ident = EnvironmentIdentity(
            adapter_kind=EnvironmentAdapterKind.NATIVE_REVIEWED,
            adapter_version="1",
            platform=platform.system().lower(),
            architecture=platform.machine().lower(),
            python_version=sys.version.split()[0],
            runtime_version=f"python/{sys.version.split()[0]}",
            tools=tuple(tools),
            lockfile_digests=lockfile_digests,
            manifest_digests=manifest_digests,
            approved_env_var_names=self._approved_env_var_names,
            network_policy=self._network_policy,
            filesystem_capability=self._filesystem_capability,
            working_directory_policy_hash=wd_policy_hash,
        )
        self._cached_identity = ident
        return ident

    def execute(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        input_text: str | None = None,
        timeout: int | None = None,
        check: bool = True,
        extra_env: Mapping[str, str] | None = None,
        output_limit: int | None = None,
    ) -> ExecutionReceipt:
        """Execute an approved command and return a receipt bound to the current identity."""
        ident = self.identity()
        result = self._executor.run(
            argv,
            cwd=cwd,
            input_text=input_text,
            timeout=timeout,
            check=check,
            extra_env=extra_env,
            output_limit=output_limit,
        )
        return ExecutionReceipt(
            argv=tuple(argv),
            identity_hash=ident.identity_hash,
            result=result,
            working_directory=str(cwd),
        )

    def collect_artifacts(
        self, artifact_paths: Sequence[str], *, cwd: Path
    ) -> tuple[ArtifactResult, ...]:
        """Collect declared artifacts after execution."""
        artifacts: list[ArtifactResult] = []
        for relative_path in artifact_paths:
            full_path = (cwd / relative_path).resolve()
            if not full_path.exists():
                continue
            try:
                digest = hashlib.sha256(full_path.read_bytes()).hexdigest()
                size = full_path.stat().st_size
                artifacts.append(
                    ArtifactResult(
                        path=relative_path,
                        size_bytes=size,
                        digest=digest,
                    )
                )
            except OSError:
                continue
        return tuple(artifacts)

    def cleanup(self, *, cwd: Path) -> None:
        """Cleanup — idempotent no-op for native execution."""
        _ = cwd  # No temp state to clean up
