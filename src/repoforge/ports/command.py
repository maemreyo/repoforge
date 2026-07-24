"""Command execution boundary."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ..domain.failure_artifacts import FailureLocation
from .cancellation import CancellationToken


@dataclass(frozen=True)
class CommandResult:
    argv: tuple[str, ...]
    cwd: str
    returncode: int
    stdout: str
    stderr: str
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    failed_selectors: tuple[str, ...] = ()
    output_artifact_reference: str | None = None
    failure_provider: str | None = None
    selector_coverage: str = "not_applicable"
    selectors_unavailable_reason: str | None = None
    failure_locations: tuple[FailureLocation, ...] = ()
    output_artifact_status: str = "not_applicable"

    @property
    def combined(self) -> str:
        parts = [part.strip() for part in (self.stdout, self.stderr) if part.strip()]
        return "\n".join(parts)


class CommandExecutor(Protocol):
    def environment(self, extra: Mapping[str, str] | None = None) -> dict[str, str]: ...

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        input_text: str | None = None,
        timeout: int | None = None,
        check: bool = True,
        extra_env: Mapping[str, str] | None = None,
        output_limit: int | None = None,
        cancel_token: CancellationToken | None = None,
    ) -> CommandResult: ...

    def run_bytes(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        timeout: int | None = None,
        max_bytes: int,
    ) -> bytes: ...
