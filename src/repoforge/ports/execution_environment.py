"""Execution environment port — abstract boundary for approved command execution."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from repoforge.domain.execution_environment import EnvironmentIdentity
from repoforge.ports.command import CommandResult


@dataclass(frozen=True)
class ArtifactResult:
    """Declared artifact collected after execution."""

    path: str
    size_bytes: int
    digest: str
    kind: str = "file"


@dataclass(frozen=True)
class ExecutionReceipt:
    """Receipt bound to a single command execution."""

    argv: tuple[str, ...]
    identity_hash: str
    result: CommandResult
    artifacts: tuple[ArtifactResult, ...] = ()
    working_directory: str = ""
    mutation_detected: bool = False


class ExecutionEnvironmentPort(Protocol):
    """Typed environment boundary for approved command execution.

    Each environment provides doctor/prepare/identity/execute/cleanup lifecycle
    and binds every execution to an environment identity hash for receipts.
    """

    def doctor(self) -> tuple[str, ...]:
        """Return health warnings about the environment (empty = healthy)."""
        ...

    def prepare(self, *, cwd: Path, extra_env: Mapping[str, str] | None = None) -> None:
        """Prepare the environment for execution.

        Idempotent. Must not modify source outside declared policy.
        """
        ...

    def identity(self) -> EnvironmentIdentity:
        """Fingerprint the current execution environment.

        Must be deterministic, secret-free, and safe for audit/receipts.
        """
        ...

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
        """Execute an approved command in this environment.

        Returns a receipt bound to the current environment identity.
        """
        ...

    def collect_artifacts(
        self, artifact_paths: Sequence[str], *, cwd: Path
    ) -> tuple[ArtifactResult, ...]:
        """Collect declared artifacts from the workspace after execution."""
        ...

    def cleanup(self, *, cwd: Path) -> None:
        """Clean up temporary or environment-specific state.

        Idempotent. Must not modify source outside declared policy.
        """
        ...
