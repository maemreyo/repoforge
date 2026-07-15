"""Execution environment port — abstract boundary for approved command execution."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ..domain.execution_environment import EnvironmentIdentity, EnvironmentIdentityRequest
from .command import CommandResult


@dataclass(frozen=True, slots=True)
class ArtifactResult:
    """Declared artifact collected after execution."""

    path: str
    size_bytes: int
    digest: str
    kind: str = "file"


@dataclass(frozen=True, slots=True)
class ExecutionReceipt:
    """Receipt bound to a single command execution."""

    argv: tuple[str, ...]
    identity_hash: str
    result: CommandResult
    artifacts: tuple[ArtifactResult, ...] = ()
    mutation_detected: bool = False


@dataclass(frozen=True, slots=True)
class ApprovedExecution:
    """One profile-approved command bound to a precomputed identity."""

    argv: tuple[str, ...]
    request: EnvironmentIdentityRequest
    identity: EnvironmentIdentity
    timeout: int


class ExecutionEnvironmentPort(Protocol):
    """Typed environment boundary for approved command execution.

    Each environment provides doctor/prepare/identity/execute/cleanup lifecycle
    and binds every execution to an environment identity hash for receipts.
    """

    def doctor(self, request: EnvironmentIdentityRequest) -> tuple[str, ...]:
        """Return health warnings about the environment (empty = healthy)."""
        ...

    def prepare(self, request: EnvironmentIdentityRequest) -> None:
        """Prepare the environment for execution.

        Idempotent. Must not modify source outside declared policy.
        """
        ...

    def identity(self, request: EnvironmentIdentityRequest) -> EnvironmentIdentity:
        """Fingerprint the current execution environment for an optional workspace.

        Must be deterministic, secret-free, and safe for audit/receipts.
        """
        ...

    def execute(
        self,
        execution: ApprovedExecution,
    ) -> ExecutionReceipt:
        """Execute an approved command in this environment.

        Returns a receipt bound to the current environment identity.
        """
        ...

    def collect_artifacts(
        self, artifact_paths: Sequence[str], *, workspace_root: Path
    ) -> tuple[ArtifactResult, ...]:
        """Collect declared artifacts from the workspace after execution."""
        ...

    def cleanup(self, request: EnvironmentIdentityRequest) -> None:
        """Clean up temporary or environment-specific state.

        Idempotent. Must not modify source outside declared policy.
        """
        ...
