"""Bounded read-only view of the ambient auth state a migration must report, not adopt.

Every read here uses `git config --show-origin --get-all <key>`, which never writes and
attributes each value to the scope it came from, so a plan can tell the operator exactly where
to look. There is deliberately no method that passes `--global`, `--system`, `--replace-all`, or
any other mutating flag, and environment variables are reported by name only -- a value that
would authenticate must never be copied into a finding, a plan, or a configuration file.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol

from ...domain.errors import ErrorCode, RepoForgeError
from ...ports.command import CommandResult

_CONFIG_KEY = re.compile(r"^[a-zA-Z][a-zA-Z0-9]*(?:\.[a-zA-Z0-9_.-]+)*\.[a-zA-Z][a-zA-Z0-9]*$")
_MAX_OUTPUT_BYTES = 100_000
_MAX_VALUES = 32
_MAX_VALUE_LENGTH = 4_096


class _IsolatedExecutor(Protocol):
    def environment(self, extra: Mapping[str, str] | None = None) -> dict[str, str]: ...

    def run_isolated(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        environment: Mapping[str, str],
        secrets: Sequence[str],
        input_text: str | None = None,
        timeout: int | None = None,
        check: bool = True,
        output_limit: int | None = None,
        cancel_token: Any | None = None,
    ) -> CommandResult: ...


class GitAmbientAuthConflictReader:
    """Report ambient Git configuration and environment names without touching either."""

    def __init__(
        self,
        executor: _IsolatedExecutor,
        *,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self._executor = executor
        self._environ = dict(environ) if environ is not None else dict(os.environ)

    def environment_names(self) -> tuple[str, ...]:
        """Return the names present in the environment; values are never read out."""

        return tuple(sorted(self._environ))

    def git_config_values(self, cwd: Path, key: str) -> tuple[tuple[str, str], ...]:
        """Return `(origin, value)` pairs for one key, or nothing when it is unset."""

        if not isinstance(key, str) or _CONFIG_KEY.fullmatch(key) is None or len(key) > 200:
            raise RepoForgeError(
                "Only a safe dotted Git configuration key can be inspected.",
                code=ErrorCode.SECURITY_POLICY_VIOLATION,
                retryable=False,
                unchanged_state=("No Git configuration was read or modified.",),
            )
        try:
            result = self._executor.run_isolated(
                ["git", "config", "--show-origin", "--get-all", key],
                cwd=cwd,
                environment=self._executor.environment(),
                secrets=(),
                output_limit=_MAX_OUTPUT_BYTES,
            )
        except RepoForgeError:
            # `git config --get-all` exits non-zero when the key is simply not set.
            return ()
        if result.stdout_truncated or len(result.stdout) > _MAX_OUTPUT_BYTES:
            return ()
        values: list[tuple[str, str]] = []
        for line in result.stdout.splitlines()[:_MAX_VALUES]:
            origin, separator, value = line.partition("\t")
            if not separator or not origin or not value:
                # Without an origin the value cannot be attributed to a scope, so reporting it
                # would tell the operator nothing actionable.
                continue
            if len(origin) > _MAX_VALUE_LENGTH or len(value) > _MAX_VALUE_LENGTH:
                continue
            values.append((origin, value))
        return tuple(values)
