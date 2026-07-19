"""Compile reviewed command surfaces into backend-neutral execution requests."""

from __future__ import annotations

from pathlib import Path

from ...domain.execution_environment import (
    CommandFailureMode,
    EnforcementRequirement,
    ExecutionScope,
    ExecutionScopeKind,
    FilesystemAccess,
    NetworkAccess,
    RequestedExecutionPolicy,
)
from ...ports.cancellation import CancellationToken
from ...ports.execution_environment import ExecutionRequest


def _request(
    *,
    workspace_id: str,
    workspace_root: Path,
    command_cwd: Path,
    commands: tuple[tuple[str, ...], ...],
    working_directory_policy: str,
    timeout_seconds: int,
    output_limit: int,
    filesystem: FilesystemAccess,
    failure_mode: CommandFailureMode,
    artifact_paths: tuple[str, ...] = (),
    cancel_token: CancellationToken | None = None,
) -> ExecutionRequest:
    return ExecutionRequest(
        scope=ExecutionScope(
            kind=ExecutionScopeKind.WORKSPACE,
            root=workspace_root,
            command_cwd=command_cwd,
            workspace_id=workspace_id,
            working_directory_policy=working_directory_policy,
        ),
        reviewed_commands=commands,
        requested_policy=RequestedExecutionPolicy(
            network=NetworkAccess.OFFLINE,
            filesystem=filesystem,
            enforcement_requirement=EnforcementRequirement.ADVISORY_BACKEND_ALLOWED,
        ),
        timeout_seconds=timeout_seconds,
        output_limit=output_limit,
        artifact_paths=artifact_paths,
        failure_mode=failure_mode,
        cancel_token=cancel_token,
    )


def profile_execution_request(
    *,
    workspace_id: str,
    workspace_root: Path,
    command_cwd: Path,
    commands: tuple[tuple[str, ...], ...],
    working_directory_policy: str,
    timeout_seconds: int,
    output_limit: int,
    cancel_token: CancellationToken | None = None,
) -> ExecutionRequest:
    return _request(
        workspace_id=workspace_id,
        workspace_root=workspace_root,
        command_cwd=command_cwd,
        commands=commands,
        working_directory_policy=working_directory_policy,
        timeout_seconds=timeout_seconds,
        output_limit=output_limit,
        filesystem=FilesystemAccess.WORKSPACE_WRITE,
        failure_mode=CommandFailureMode.RAISE,
        cancel_token=cancel_token,
    )


def diagnostic_execution_request(
    *,
    workspace_id: str,
    workspace_root: Path,
    command_cwd: Path,
    argv: tuple[str, ...],
    working_directory_policy: str,
    timeout_seconds: int,
    output_limit: int,
    read_only: bool,
    artifact_paths: tuple[str, ...],
    cancel_token: CancellationToken | None = None,
) -> ExecutionRequest:
    return _request(
        workspace_id=workspace_id,
        workspace_root=workspace_root,
        command_cwd=command_cwd,
        commands=(argv,),
        working_directory_policy=working_directory_policy,
        timeout_seconds=timeout_seconds,
        output_limit=output_limit,
        filesystem=(
            FilesystemAccess.SOURCE_READ if read_only else FilesystemAccess.WORKSPACE_WRITE
        ),
        failure_mode=CommandFailureMode.RETURN,
        artifact_paths=artifact_paths,
        cancel_token=cancel_token,
    )


def adhoc_execution_request(
    *,
    workspace_id: str,
    workspace_root: Path,
    command_cwd: Path,
    argv: tuple[str, ...],
    working_directory_policy: str,
    timeout_seconds: int,
    output_limit: int,
    cancel_token: CancellationToken | None = None,
) -> ExecutionRequest:
    return _request(
        workspace_id=workspace_id,
        workspace_root=workspace_root,
        command_cwd=command_cwd,
        commands=(argv,),
        working_directory_policy=working_directory_policy,
        timeout_seconds=timeout_seconds,
        output_limit=output_limit,
        filesystem=FilesystemAccess.WORKSPACE_WRITE,
        failure_mode=CommandFailureMode.RETURN,
        cancel_token=cancel_token,
    )
