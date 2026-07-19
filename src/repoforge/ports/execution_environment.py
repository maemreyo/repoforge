"""Typed execution environment boundary and session contracts."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ..domain.execution_environment import (
    CommandFailureMode,
    EffectiveExecutionPolicy,
    EnvironmentIdentity,
    EnvironmentIdentityRequest,
    ExecutionScope,
    RequestedExecutionPolicy,
)
from .cancellation import CancellationToken
from .command import CommandResult


@dataclass(frozen=True, slots=True)
class ArtifactResult:
    path: str
    size_bytes: int
    digest: str
    kind: str = "file"


@dataclass(frozen=True, slots=True)
class ExecutionRequest:
    scope: ExecutionScope
    reviewed_commands: tuple[tuple[str, ...], ...]
    requested_policy: RequestedExecutionPolicy
    timeout_seconds: int
    output_limit: int
    artifact_paths: tuple[str, ...] = ()
    failure_mode: CommandFailureMode = CommandFailureMode.RAISE
    cancel_token: CancellationToken | None = None
    lockfiles: tuple[str, ...] = (
        "uv.lock",
        "poetry.lock",
        "Pipfile.lock",
        "pnpm-lock.yaml",
        "yarn.lock",
        "package-lock.json",
        "Cargo.lock",
        "go.sum",
        "Gemfile.lock",
    )
    manifests: tuple[str, ...] = (
        "pyproject.toml",
        "package.json",
        "Cargo.toml",
        "go.mod",
    )

    def __post_init__(self) -> None:
        if not self.reviewed_commands:
            raise ValueError("reviewed_commands must not be empty")
        if any(
            not command or any(not item for item in command) for command in self.reviewed_commands
        ):
            raise ValueError("reviewed_commands must contain non-empty argv values")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if self.output_limit <= 0:
            raise ValueError("output_limit must be positive")

    @property
    def tools(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(command[0] for command in self.reviewed_commands))


@dataclass(frozen=True, slots=True)
class PreparedEnvironmentSession:
    session_id: str
    identity: EnvironmentIdentity
    requested_policy_hash: str
    effective_policy: EffectiveExecutionPolicy
    effective_policy_hash: str


@dataclass(frozen=True, slots=True)
class EnvironmentInspection:
    identity: EnvironmentIdentity
    requested_policy_hash: str
    effective_policy: EffectiveExecutionPolicy
    effective_policy_hash: str
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ExecutionReceipt:
    argv: tuple[str, ...]
    session_start_identity_hash: str
    result: CommandResult
    requested_policy_hash: str = ""
    effective_policy_hash: str = ""
    effective_policy: EffectiveExecutionPolicy | None = None
    artifacts: tuple[ArtifactResult, ...] = ()

    @property
    def identity_hash(self) -> str:
        return self.session_start_identity_hash


# Compatibility envelope retained only while callers are migrated to ExecutionCoordinator.
@dataclass(frozen=True, slots=True)
class ApprovedExecution:
    argv: tuple[str, ...]
    request: EnvironmentIdentityRequest
    identity: EnvironmentIdentity
    timeout: int
    cancel_token: CancellationToken | None = None


class ExecutionEnvironmentPort(Protocol):
    """Private backend contract used only by the execution coordinator."""

    def prepare_session(self, request: ExecutionRequest) -> PreparedEnvironmentSession: ...

    def inspect_session(
        self,
        request: ExecutionRequest,
        session: PreparedEnvironmentSession | None = None,
    ) -> EnvironmentInspection: ...

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
    ) -> CommandResult: ...

    def execute_bytes_in_session(
        self,
        session: PreparedEnvironmentSession,
        argv: tuple[str, ...],
        *,
        cwd: Path,
        timeout: int,
        max_bytes: int,
    ) -> bytes: ...

    def collect_session_artifacts(
        self,
        session: PreparedEnvironmentSession,
        artifact_paths: Sequence[str],
        *,
        root: Path,
    ) -> tuple[ArtifactResult, ...]: ...

    def cleanup_session(self, session: PreparedEnvironmentSession) -> None: ...

    # Transitional methods. No application caller may use these after routing migration.
    def doctor(self, request: EnvironmentIdentityRequest) -> tuple[str, ...]: ...

    def prepare(self, request: EnvironmentIdentityRequest) -> None: ...

    def identity(self, request: EnvironmentIdentityRequest) -> EnvironmentIdentity: ...

    def execute(self, execution: ApprovedExecution) -> ExecutionReceipt: ...

    def collect_artifacts(
        self, artifact_paths: Sequence[str], *, workspace_root: Path
    ) -> tuple[ArtifactResult, ...]: ...

    def cleanup(self, request: EnvironmentIdentityRequest) -> None: ...
