"""Launch the long-lived RepoForge supervisor worker without shell interpolation."""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from ...domain.runtime import RuntimeRecord
from .state_store import process_identity


class SubprocessRuntimeLauncher:
    def start(self, config_path: Path, *, foreground: bool, extra_env: dict[str, str]) -> int:
        argv = [
            sys.executable,
            "-m",
            "repoforge.interfaces.runtime.worker",
            "--config",
            str(config_path),
        ]
        inherited = (
            "HOME",
            "PATH",
            "LANG",
            "LC_ALL",
            "SSH_AUTH_SOCK",
            "HTTPS_PROXY",
            "HTTP_PROXY",
            "NO_PROXY",
            "CONTROL_PLANE_API_KEY",
            "PYTHONPATH",
            "VIRTUAL_ENV",
        )
        env = {key: os.environ[key] for key in inherited if key in os.environ}
        env.update(extra_env)
        if foreground:
            completed = subprocess.run(argv, env=env, check=False)
            return completed.returncode
        log = config_path.parent / ".repoforge-supervisor-launch.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("ab") as handle:
            process = subprocess.Popen(
                argv,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        return process.pid

    def force_stop(self, record: RuntimeRecord, *, grace_seconds: float = 5.0) -> bool:
        """Terminate only the identity-bound supervisor process group."""
        if (
            record.pid is None
            or record.process_identity is None
            or process_identity(record.pid) != record.process_identity
        ):
            return False
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(record.pid, signal.SIGTERM)
        deadline = time.monotonic() + max(0.0, grace_seconds)
        while time.monotonic() < deadline:
            if process_identity(record.pid) != record.process_identity:
                return True
            time.sleep(0.05)
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(record.pid, signal.SIGKILL)
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            if process_identity(record.pid) != record.process_identity:
                return True
            time.sleep(0.05)
        return False
