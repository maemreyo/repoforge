"""Truthful native execution adapter over RepoForge's constrained executor."""

from __future__ import annotations

import hashlib
import json
import platform
import sys
from collections.abc import Sequence
from pathlib import Path

from ...domain.errors import ErrorCode, RepoForgeError, SecurityError
from ...domain.execution_environment import (
    NATIVE_ADVISORY_ENFORCEMENT,
    EffectiveExecutionPolicy,
    EffectiveResourceLimits,
    EnforcementRequirement,
    EnvironmentAdapterKind,
    EnvironmentIdentity,
    EnvironmentIdentityRequest,
    FilesystemAccess,
    FilesystemCapability,
    NetworkAccess,
    NetworkPolicy,
    RequestedExecutionPolicy,
)
from ...ports.cancellation import CancellationToken
from ...ports.command import CommandExecutor, CommandResult
from ...ports.execution_environment import (
    ApprovedExecution,
    ArtifactResult,
    EnvironmentInspection,
    ExecutionReceipt,
    ExecutionRequest,
    PreparedEnvironmentSession,
)
from .native_identity import (
    collect_environment_hashes,
    collect_file_digests,
    resolve_tools,
)

_ADAPTER_VERSION = "2"
_NATIVE_DEGRADATIONS = ("network_not_isolated", "filesystem_not_isolated")


def _stable_hash(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _legacy_requested_policy(request: EnvironmentIdentityRequest) -> RequestedExecutionPolicy:
    network = {
        NetworkPolicy.NONE: NetworkAccess.OFFLINE,
        NetworkPolicy.RESTRICTED: NetworkAccess.PUBLIC_HTTP_HTTPS,
        NetworkPolicy.EXTERNAL: NetworkAccess.PUBLIC_GENERAL,
    }[request.network_policy]
    filesystem = {
        FilesystemCapability.READ: FilesystemAccess.SOURCE_READ,
        FilesystemCapability.WORKSPACE_WRITE: FilesystemAccess.WORKSPACE_WRITE,
        FilesystemCapability.MANAGED_STATE_WRITE: FilesystemAccess.MANAGED_STATE_WRITE,
    }[request.filesystem_capability]
    return RequestedExecutionPolicy(network=network, filesystem=filesystem)


class NativeReviewedAdapter:
    """Execute on the host while reporting advisory host access truthfully."""

    def __init__(self, executor: CommandExecutor, *, max_artifact_bytes: int = 2_000_000) -> None:
        self._executor: CommandExecutor = executor
        self._max_artifact_bytes: int = max_artifact_bytes

    @staticmethod
    def _effective_policy(requested: RequestedExecutionPolicy) -> EffectiveExecutionPolicy:
        if requested.enforcement_requirement is EnforcementRequirement.ENFORCEMENT_REQUIRED:
            raise RepoForgeError(
                "Native reviewed execution cannot enforce the requested isolation policy",
                code=ErrorCode.EXECUTION_POLICY_UNSUPPORTED,
                unchanged_state=("No repository command was started.",),
                safe_next_action=(
                    "Select an execution backend that enforces the requested policy, or use an "
                    "explicitly reviewed advisory trust mode."
                ),
            )
        return EffectiveExecutionPolicy(
            network=NetworkAccess.HOST_INHERITED,
            filesystem=FilesystemAccess.HOST_ACCOUNT_ACCESS,
            credential_capabilities=(),
            resource_limits=EffectiveResourceLimits(),
            enforcement=NATIVE_ADVISORY_ENFORCEMENT,
            degraded=True,
            degradation_reasons=_NATIVE_DEGRADATIONS,
        )

    @staticmethod
    def _backend_capability_hash(effective: EffectiveExecutionPolicy) -> str:
        return _stable_hash(
            {
                "adapter_kind": EnvironmentAdapterKind.NATIVE_REVIEWED.value,
                "adapter_version": _ADAPTER_VERSION,
                "effective_policy": effective.policy_hash,
            }
        )

    def _identity(
        self,
        *,
        root: Path,
        command_cwd: Path,
        commands: tuple[tuple[str, ...], ...],
        working_directory_policy: str,
        lockfiles: tuple[str, ...],
        manifests: tuple[str, ...],
        requested: RequestedExecutionPolicy,
        effective: EffectiveExecutionPolicy,
    ) -> EnvironmentIdentity:
        legacy = EnvironmentIdentityRequest(
            workspace_root=root,
            command_cwd=command_cwd,
            commands=commands,
            working_directory_policy=working_directory_policy,
            lockfiles=lockfiles,
            manifests=manifests,
        )
        environment = self._executor.environment()
        return EnvironmentIdentity(
            adapter_kind=EnvironmentAdapterKind.NATIVE_REVIEWED,
            adapter_version=_ADAPTER_VERSION,
            platform=platform.system().lower(),
            architecture=platform.machine().lower(),
            python_version=sys.version.split()[0],
            runtime_version=f"python/{sys.version.split()[0]}",
            tools=resolve_tools(self._executor, legacy),
            lockfile_digests=collect_file_digests(root, lockfiles),
            manifest_digests=collect_file_digests(root, manifests),
            approved_env_var_names=tuple(sorted(environment)),
            approved_env_value_hashes=collect_environment_hashes(environment),
            requested_policy_hash=requested.policy_hash,
            effective_policy_hash=effective.policy_hash,
            effective_network=effective.network,
            effective_filesystem=effective.filesystem,
            enforcement_assessment=effective.enforcement,
            backend_capability_hash=self._backend_capability_hash(effective),
            working_directory_policy_hash=hashlib.sha256(
                working_directory_policy.encode("utf-8")
            ).hexdigest(),
        )

    def prepare_session(self, request: ExecutionRequest) -> PreparedEnvironmentSession:
        effective = self._effective_policy(request.requested_policy)
        identity = self._identity(
            root=request.scope.root,
            command_cwd=request.scope.command_cwd,
            commands=request.reviewed_commands,
            working_directory_policy=request.scope.working_directory_policy,
            lockfiles=request.lockfiles,
            manifests=request.manifests,
            requested=request.requested_policy,
            effective=effective,
        )
        session_id = _stable_hash(
            {
                "identity_hash": identity.identity_hash,
                "requested_policy_hash": request.requested_policy.policy_hash,
            }
        )[:32]
        return PreparedEnvironmentSession(
            session_id=session_id,
            identity=identity,
            requested_policy_hash=request.requested_policy.policy_hash,
            effective_policy=effective,
            effective_policy_hash=effective.policy_hash,
        )

    def inspect_session(
        self,
        request: ExecutionRequest,
        session: PreparedEnvironmentSession | None = None,
    ) -> EnvironmentInspection:
        effective = self._effective_policy(request.requested_policy)
        identity = self._identity(
            root=request.scope.root,
            command_cwd=request.scope.command_cwd,
            commands=request.reviewed_commands,
            working_directory_policy=request.scope.working_directory_policy,
            lockfiles=request.lockfiles,
            manifests=request.manifests,
            requested=request.requested_policy,
            effective=effective,
        )
        warnings = tuple(
            f"Reviewed tool {tool.name!r} is unavailable or has no inspectable version"
            for tool in identity.tools
            if tool.version is None
        )
        if session is not None and session.effective_policy_hash != effective.policy_hash:
            raise RepoForgeError(
                "Native execution policy changed during the prepared session",
                code=ErrorCode.EXECUTION_ENVIRONMENT_DRIFT,
                unchanged_state=("No additional repository command was started.",),
            )
        return EnvironmentInspection(
            identity=identity,
            requested_policy_hash=request.requested_policy.policy_hash,
            effective_policy=effective,
            effective_policy_hash=effective.policy_hash,
            warnings=warnings,
        )

    def execute_in_session(
        self,
        session: PreparedEnvironmentSession,
        argv: tuple[str, ...],
        *,
        cwd: Path,
        timeout: int,
        output_limit: int,
        check: bool,
        cancel_token: CancellationToken | None = None,
    ) -> CommandResult:
        _ = session
        if cancel_token is None:
            return self._executor.run(
                argv,
                cwd=cwd,
                timeout=timeout,
                check=check,
                output_limit=output_limit,
            )
        return self._executor.run(
            argv,
            cwd=cwd,
            timeout=timeout,
            check=check,
            output_limit=output_limit,
            cancel_token=cancel_token,
        )

    def collect_session_artifacts(
        self,
        session: PreparedEnvironmentSession,
        artifact_paths: Sequence[str],
        *,
        root: Path,
    ) -> tuple[ArtifactResult, ...]:
        _ = session
        return self.collect_artifacts(artifact_paths, workspace_root=root)

    def cleanup_session(self, session: PreparedEnvironmentSession) -> None:
        _ = session

    # Transitional compatibility methods used until all application callers route
    # through ExecutionCoordinator.
    def doctor(self, request: EnvironmentIdentityRequest) -> tuple[str, ...]:
        return tuple(
            f"Reviewed tool {tool.name!r} is unavailable or has no inspectable version"
            for tool in resolve_tools(self._executor, request)
            if tool.version is None
        )

    def prepare(self, request: EnvironmentIdentityRequest) -> None:
        _ = request

    def identity(self, request: EnvironmentIdentityRequest) -> EnvironmentIdentity:
        requested = _legacy_requested_policy(request)
        effective = self._effective_policy(requested)
        return self._identity(
            root=request.workspace_root,
            command_cwd=request.command_cwd,
            commands=request.commands,
            working_directory_policy=request.working_directory_policy,
            lockfiles=request.lockfiles,
            manifests=request.manifests,
            requested=requested,
            effective=effective,
        )

    def execute(self, execution: ApprovedExecution) -> ExecutionReceipt:
        requested = _legacy_requested_policy(execution.request)
        effective = self._effective_policy(requested)
        if execution.cancel_token is None:
            result = self._executor.run(
                execution.argv,
                cwd=execution.request.command_cwd,
                timeout=execution.timeout,
            )
        else:
            result = self._executor.run(
                execution.argv,
                cwd=execution.request.command_cwd,
                timeout=execution.timeout,
                cancel_token=execution.cancel_token,
            )
        return ExecutionReceipt(
            execution.argv,
            execution.identity.identity_hash,
            result,
            requested.policy_hash,
            effective.policy_hash,
            effective,
        )

    def collect_artifacts(
        self, artifact_paths: Sequence[str], *, workspace_root: Path
    ) -> tuple[ArtifactResult, ...]:
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
        _ = request
