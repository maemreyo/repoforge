"""Run one exact child process with a broker-issued repository-auth context."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from ...domain.repository_auth_broker import ProcessAuthContext
from ...ports.cancellation import CancellationToken
from ...ports.command import CommandResult
from .command_executor import SubprocessCommandExecutor


class SubprocessAuthRunner:
    def __init__(self, executor: SubprocessCommandExecutor) -> None:
        self._executor = executor

    def run(
        self,
        context: ProcessAuthContext,
        argv: Sequence[str],
        *,
        cwd: Path,
        input_text: str | None = None,
        timeout: int | None = None,
        check: bool = True,
        output_limit: int | None = None,
        cancel_token: CancellationToken | None = None,
    ) -> CommandResult:
        return self._executor.run_isolated(
            argv,
            cwd=cwd,
            environment=context.environment_dict(),
            secrets=context.secret_values,
            input_text=input_text,
            timeout=timeout,
            check=check,
            output_limit=output_limit,
            cancel_token=cancel_token,
        )
