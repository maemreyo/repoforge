"""Bounded subprocess adapter with process-group timeout cleanup."""

from __future__ import annotations
import os
import signal
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from ...config import ServerConfig
from ...domain.errors import CommandError
from ...ports.command import CommandResult


class SubprocessCommandExecutor:
    def __init__(self, config: ServerConfig):
        self.config = config

    def environment(self, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        env = {
            k: os.environ[k] for k in self.config.allowed_environment if k in os.environ
        }
        inherited = env.get("PATH", "")
        parts = [*self.config.path_prefixes]
        if inherited:
            parts.append(inherited)
        env["PATH"] = os.pathsep.join(dict.fromkeys((p for p in parts if p)))
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
        removed = len(text) - half * 2
        return (
            f"{text[:half]}\n\n... <{removed} characters omitted> ...\n\n{text[-half:]}"
        )

    def _communicate(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        input_data: str | bytes | None,
        text: bool,
        timeout: int,
        extra_env: Mapping[str, str] | None,
    ) -> tuple[subprocess.Popen[Any], tuple[str | bytes, str | bytes]]:
        try:
            process = subprocess.Popen(
                list(argv),
                cwd=cwd,
                env=self.environment(extra_env),
                stdin=subprocess.PIPE if input_data is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=text,
                encoding="utf-8" if text else None,
                errors="replace" if text else None,
                start_new_session=True,
            )
        except FileNotFoundError as exc:
            raise CommandError(f"Executable not found: {argv[0]}") from exc
        except OSError as exc:
            raise CommandError(f"Cannot execute {' '.join(argv)}: {exc}") from exc
        try:
            return (process, process.communicate(input_data, timeout=timeout))
        except subprocess.TimeoutExpired as exc:
            try:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=2)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            process.communicate()
            raise CommandError(
                f"Command timed out after {timeout}s: {' '.join(argv)}"
            ) from exc

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
        if not argv or not all((isinstance(x, str) and x for x in argv)):
            raise CommandError("Command argv must contain non-empty strings")
        actual_timeout = timeout or self.config.default_command_timeout_seconds
        limit = output_limit or self.config.max_tool_output_chars
        process, (stdout, stderr) = self._communicate(
            argv,
            cwd=cwd,
            input_data=input_text,
            text=True,
            timeout=actual_timeout,
            extra_env=extra_env,
        )
        if not isinstance(stdout, str) or not isinstance(stderr, str):
            raise CommandError("Text command returned binary output")
        result = CommandResult(
            tuple(argv),
            str(cwd),
            process.returncode or 0,
            self._truncate(stdout, limit),
            self._truncate(stderr, limit),
        )
        if check and result.returncode != 0:
            raise CommandError(
                f"Command failed with exit code {result.returncode}: {' '.join(argv)}\n{result.combined or '<no output>'}"
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
        actual_timeout = timeout or self.config.default_command_timeout_seconds
        process, (stdout, stderr) = self._communicate(
            argv,
            cwd=cwd,
            input_data=None,
            text=False,
            timeout=actual_timeout,
            extra_env=None,
        )
        if not isinstance(stdout, bytes) or not isinstance(stderr, bytes):
            raise CommandError("Binary command returned text output")
        if process.returncode != 0:
            raise CommandError(
                f"Command failed with exit code {process.returncode}: {' '.join(argv)}\n{stderr.decode('utf-8', errors='replace')}"
            )
        if len(stdout) > max_bytes:
            raise CommandError(
                f"Command output exceeds fingerprint limit of {max_bytes} bytes: {' '.join(argv)}"
            )
        return stdout


CommandRunner = SubprocessCommandExecutor
