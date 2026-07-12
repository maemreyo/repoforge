"""Safe subprocess execution without shell interpolation."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from .config import ServerConfig
from .errors import CommandError


@dataclass(frozen=True)
class CommandResult:
    argv: tuple[str, ...]
    cwd: str
    returncode: int
    stdout: str
    stderr: str

    @property
    def combined(self) -> str:
        parts = [part.strip() for part in (self.stdout, self.stderr) if part.strip()]
        return "\n".join(parts)


class CommandRunner:
    def __init__(self, config: ServerConfig):
        self.config = config

    def environment(self, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        env = {key: os.environ[key] for key in self.config.allowed_environment if key in os.environ}
        inherited_path = env.get("PATH", "")
        path_parts = [*self.config.path_prefixes]
        if inherited_path:
            path_parts.append(inherited_path)
        env["PATH"] = os.pathsep.join(dict.fromkeys(part for part in path_parts if part))
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GH_PROMPT_DISABLED"] = "1"
        if extra:
            env.update(extra)
        return env

    @staticmethod
    def _truncate(text: str, limit: int) -> str:
        if len(text) <= limit:
            return text
        half = max(1, limit // 2)
        removed = len(text) - (half * 2)
        return f"{text[:half]}\n\n... <{removed} characters omitted> ...\n\n{text[-half:]}"

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
    ) -> CommandResult:
        if not argv or not all(isinstance(arg, str) and arg for arg in argv):
            raise CommandError("Command argv must contain non-empty strings")
        timeout = timeout or self.config.default_command_timeout_seconds
        limit = output_limit or self.config.max_tool_output_chars
        try:
            completed = subprocess.run(
                list(argv),
                cwd=cwd,
                env=self.environment(extra_env),
                input=input_text,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                timeout=timeout,
                check=False,
            )
        except FileNotFoundError as exc:
            raise CommandError(f"Executable not found: {argv[0]}") from exc
        except subprocess.TimeoutExpired as exc:
            raise CommandError(f"Command timed out after {timeout}s: {' '.join(argv)}") from exc
        except OSError as exc:
            raise CommandError(f"Cannot execute {' '.join(argv)}: {exc}") from exc

        result = CommandResult(
            argv=tuple(argv),
            cwd=str(cwd),
            returncode=completed.returncode,
            stdout=self._truncate(completed.stdout, limit),
            stderr=self._truncate(completed.stderr, limit),
        )
        if check and result.returncode != 0:
            detail = result.combined or "<no output>"
            raise CommandError(
                f"Command failed with exit code {result.returncode}: {' '.join(argv)}\n{detail}"
            )
        return result

    def run_bytes(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        timeout: int | None = None,
        max_bytes: int,
    ) -> bytes:
        timeout = timeout or self.config.default_command_timeout_seconds
        try:
            completed = subprocess.run(
                list(argv),
                cwd=cwd,
                env=self.environment(),
                capture_output=True,
                timeout=timeout,
                check=False,
            )
        except FileNotFoundError as exc:
            raise CommandError(f"Executable not found: {argv[0]}") from exc
        except subprocess.TimeoutExpired as exc:
            raise CommandError(f"Command timed out after {timeout}s: {' '.join(argv)}") from exc
        if completed.returncode != 0:
            detail = completed.stderr.decode("utf-8", errors="replace")
            raise CommandError(
                f"Command failed with exit code {completed.returncode}: {' '.join(argv)}\n{detail}"
            )
        if len(completed.stdout) > max_bytes:
            raise CommandError(
                f"Command output exceeds fingerprint limit of {max_bytes} bytes: {' '.join(argv)}"
            )
        return completed.stdout
