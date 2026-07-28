"""Subprocess lifecycle for the isolated durable execution worker."""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from ...domain.errors import ConfigError
from ...domain.runtime import ChildProcess
from .state_store import process_identity


class SubprocessExecutionWorker:
    def __init__(self, config_path: Path) -> None:
        self._config_path = config_path.expanduser().resolve()
        self._children: dict[int, subprocess.Popen[bytes]] = {}

    def start(
        self,
        generation: int,
        *,
        env: dict[str, str],
        log_path: Path,
        correlation_id: str | None = None,
    ) -> ChildProcess:
        del correlation_id
        if generation <= 0:
            raise ConfigError("Execution worker generation must be positive")
        log_path.parent.mkdir(parents=True, exist_ok=True)
        argv = [
            sys.executable,
            "-m",
            "repoforge.interfaces.runtime.execution_worker",
            "--config",
            str(self._config_path),
            "--generation",
            str(generation),
        ]
        with log_path.open("ab") as log:
            process = subprocess.Popen(
                argv,
                env=dict(env),
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        identity = process_identity(process.pid)
        if identity is None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(process.pid, signal.SIGTERM)
            raise ConfigError("Could not establish execution worker process identity")
        self._children[process.pid] = process
        return ChildProcess(
            process.pid,
            identity,
            datetime.now(timezone.utc).isoformat(),
        )

    def is_alive(self, child: ChildProcess) -> bool:
        process = self._children.get(child.pid)
        return bool(
            process is not None
            and process.poll() is None
            and process_identity(child.pid) == child.process_identity
        )

    def terminate(self, child: ChildProcess, *, grace_seconds: float) -> None:
        process = self._children.get(child.pid)
        if process is None or process_identity(child.pid) != child.process_identity:
            self._children.pop(child.pid, None)
            return
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(child.pid, signal.SIGTERM)
        deadline = time.monotonic() + max(0.0, grace_seconds)
        while time.monotonic() < deadline and process.poll() is None:
            time.sleep(0.05)
        if process.poll() is None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(child.pid, signal.SIGKILL)
        self._children.pop(child.pid, None)
