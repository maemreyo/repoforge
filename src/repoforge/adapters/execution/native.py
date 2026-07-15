"""Native execution adapter over RepoForge's constrained command executor."""

from __future__ import annotations

import hashlib
import platform
import sys
from collections.abc import Sequence
from pathlib import Path

from ...domain.errors import SecurityError
from ...domain.execution_environment import (
    EnvironmentAdapterKind,
    EnvironmentIdentity,
    EnvironmentIdentityRequest,
)
from ...ports.command import CommandExecutor
from ...ports.execution_environment import ApprovedExecution, ArtifactResult, ExecutionReceipt
from .native_identity import collect_environment_hashes, collect_file_digests, resolve_tools


class NativeReviewedAdapter:
    """Delegate reviewed commands while binding receipts to native identity."""

    def __init__(self, executor: CommandExecutor, *, max_artifact_bytes: int = 2_000_000) -> None:
        self._executor: CommandExecutor = executor
        self._max_artifact_bytes: int = max_artifact_bytes

    def doctor(self, request: EnvironmentIdentityRequest) -> tuple[str, ...]:
        """Report tools whose versions cannot be inspected conservatively."""
        return tuple(
            f"Reviewed tool {tool.name!r} is unavailable or has no inspectable version"
            for tool in resolve_tools(self._executor, request)
            if tool.version is None
        )

    def prepare(self, request: EnvironmentIdentityRequest) -> None:
        """Prepare native execution; intentionally idempotent and side-effect free."""
        _ = request

    def identity(self, request: EnvironmentIdentityRequest) -> EnvironmentIdentity:
        """Fingerprint only the reviewed inputs declared by one profile."""
        environment = self._executor.environment()
        return EnvironmentIdentity(
            adapter_kind=EnvironmentAdapterKind.NATIVE_REVIEWED,
            adapter_version="1",
            platform=platform.system().lower(),
            architecture=platform.machine().lower(),
            python_version=sys.version.split()[0],
            runtime_version=f"python/{sys.version.split()[0]}",
            tools=resolve_tools(self._executor, request),
            lockfile_digests=collect_file_digests(request.workspace_root, request.lockfiles),
            manifest_digests=collect_file_digests(request.workspace_root, request.manifests),
            approved_env_var_names=tuple(sorted(environment)),
            approved_env_value_hashes=collect_environment_hashes(environment),
            network_policy=request.network_policy,
            filesystem_capability=request.filesystem_capability,
            working_directory_policy_hash=hashlib.sha256(
                request.working_directory_policy.encode("utf-8")
            ).hexdigest(),
        )

    def execute(
        self,
        execution: ApprovedExecution,
    ) -> ExecutionReceipt:
        """Execute an approved command with the precomputed profile identity."""
        result = self._executor.run(
            execution.argv,
            cwd=execution.request.command_cwd,
            timeout=execution.timeout,
        )
        return ExecutionReceipt(execution.argv, execution.identity.identity_hash, result)

    def collect_artifacts(
        self, artifact_paths: Sequence[str], *, workspace_root: Path
    ) -> tuple[ArtifactResult, ...]:
        """Collect regular declared files without following paths outside the workspace."""
        root = workspace_root.resolve(strict=True)
        artifacts: list[ArtifactResult] = []
        for relative_path in artifact_paths:
            unresolved = workspace_root / relative_path
            if unresolved.is_symlink():
                raise SecurityError(f"Artifact path cannot be a symlink: {relative_path}")
            resolved = unresolved.resolve(strict=False)
            try:
                _ = resolved.relative_to(root)
            except ValueError as exc:
                raise SecurityError(f"Artifact path escapes workspace: {relative_path}") from exc
            if not resolved.is_file():
                continue
            size = resolved.stat().st_size
            if size > self._max_artifact_bytes:
                raise SecurityError(
                    f"Artifact exceeds {self._max_artifact_bytes} byte limit: {relative_path}"
                )
            payload = resolved.read_bytes()
            artifacts.append(
                ArtifactResult(
                    path=relative_path,
                    size_bytes=size,
                    digest=hashlib.sha256(payload).hexdigest(),
                )
            )
        return tuple(artifacts)

    def cleanup(self, request: EnvironmentIdentityRequest) -> None:
        """Clean native execution state; intentionally idempotent and side-effect free."""
        _ = request
