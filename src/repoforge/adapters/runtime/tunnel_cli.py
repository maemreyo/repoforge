"""Bounded tunnel-client adapter with process-group ownership."""

from __future__ import annotations

import contextlib
import os
import shlex
import signal
import subprocess
import time
from pathlib import Path

from ...domain.errors import ConfigError
from ...domain.redaction import redact_text
from ...domain.runtime import ChildProcess, TunnelProfile
from .state_store import process_identity


class TunnelCliClient:
    def __init__(self, executable: str, *, default_timeout_seconds: int = 60):
        self.executable = executable
        self.default_timeout_seconds = default_timeout_seconds
        self._children: dict[int, subprocess.Popen[bytes]] = {}

    @staticmethod
    def _run(argv: list[str], *, env: dict[str, str], timeout: int) -> tuple[int, str]:
        try:
            completed = subprocess.run(
                argv, env=env, capture_output=True, check=False, timeout=timeout
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ConfigError(f"Tunnel command failed to execute: {exc}") from exc
        output = (completed.stdout + completed.stderr).decode("utf-8", errors="replace")
        redacted = redact_text(output, secrets=(env.get("CONTROL_PLANE_API_KEY", ""),))
        if completed.returncode != 0:
            raise ConfigError(
                f"Tunnel command failed with exit code {completed.returncode}: {redacted}"
            )
        return completed.returncode, redacted

    def executable_version(self) -> str:
        try:
            completed = subprocess.run(
                [self.executable, "--version"], capture_output=True, check=False, timeout=10
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ConfigError(f"Cannot inspect tunnel-client version: {exc}") from exc
        if completed.returncode != 0:
            raise ConfigError(
                "Cannot inspect tunnel-client version: "
                + redact_text(
                    (completed.stdout + completed.stderr).decode("utf-8", errors="replace")
                )
            )
        return redact_text(
            (completed.stdout + completed.stderr).decode("utf-8", errors="replace").strip()
        )

    def initialize(self, profile: TunnelProfile, *, env: dict[str, str]) -> None:
        tunnel_id = env.get("REPOFORGE_TUNNEL_ID")
        if not tunnel_id:
            raise ConfigError("Tunnel id is available only in the activation environment")
        self._run(
            [
                self.executable,
                "init",
                "--sample",
                "sample_mcp_stdio_local",
                "--profile",
                profile.profile,
                "--tunnel-id",
                tunnel_id,
                "--mcp-command",
                shlex.join(profile.mcp_argv),
            ],
            env=env,
            timeout=self.default_timeout_seconds,
        )

    def doctor(self, profile: TunnelProfile, *, env: dict[str, str]) -> tuple[bool, str]:
        try:
            _, output = self._run(
                [self.executable, "doctor", "--profile", profile.profile, "--explain"],
                env=env,
                timeout=self.default_timeout_seconds,
            )
            return True, output[-8000:]
        except ConfigError as exc:
            return False, str(exc)

    def start(self, profile: TunnelProfile, *, env: dict[str, str], log_path: Path) -> ChildProcess:
        log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if log_path.exists() and log_path.stat().st_size > 5_000_000:
            backup = log_path.with_suffix(log_path.suffix + ".1")
            backup.unlink(missing_ok=True)
            os.replace(log_path, backup)
        handle = log_path.open("ab", buffering=0)
        try:
            process = subprocess.Popen(
                [self.executable, "run", "--profile", profile.profile],
                env=env,
                start_new_session=True,
                stdin=subprocess.DEVNULL,
                stdout=handle,
                stderr=subprocess.STDOUT,
            )
        finally:
            handle.close()
        identity = process_identity(process.pid)
        if identity is None:
            process.terminate()
            raise ConfigError("Cannot bind tunnel child process identity")
        self._children[process.pid] = process
        return ChildProcess(process.pid, identity, str(time.time_ns()))

    def is_alive(self, child: ChildProcess) -> bool:
        if process_identity(child.pid) != child.process_identity:
            return False
        process = self._children.get(child.pid)
        return process is None or process.poll() is None

    def terminate(self, child: ChildProcess, *, grace_seconds: float) -> None:
        if process_identity(child.pid) != child.process_identity:
            return
        try:
            os.killpg(child.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        deadline = time.monotonic() + grace_seconds
        while time.monotonic() < deadline:
            if not self.is_alive(child):
                return
            time.sleep(0.05)
        with contextlib.suppress(ProcessLookupError):
            os.killpg(child.pid, signal.SIGKILL)
