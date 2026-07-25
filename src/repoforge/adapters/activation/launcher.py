"""Launch the supervisor through the stable release shim, not the caller's interpreter.

``SubprocessRuntimeLauncher`` spawns the worker with ``sys.executable`` -- correct for
an ordinary ``rf start``, but wrong for version activation: the CLI performing the
upgrade is itself running the *old* release, so re-using its interpreter would start
the old code again and the candidate could never converge. This launcher execs the
stable shim, which resolves through the ``current`` symlink and therefore always runs
whichever release is active.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import time
from pathlib import Path

from ...domain.errors import ConfigError
from ...domain.runtime import RuntimeRecord
from ..runtime.state_store import process_identity

_INHERITED = (
    "HOME",
    "PATH",
    "LANG",
    "LC_ALL",
    "SSH_AUTH_SOCK",
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "NO_PROXY",
    "CONTROL_PLANE_API_KEY",
)


class ReleaseAwareRuntimeLauncher:
    """Start the supervisor via ``<release-root>/bin/rf``, i.e. through ``current``."""

    def __init__(self, shim_path: Path) -> None:
        self._shim = shim_path

    def start(self, config_path: Path, *, foreground: bool, extra_env: dict[str, str]) -> int:
        if not self._shim.is_file():
            raise ConfigError(
                f"LAUNCHER_SHIM_MISSING: {self._shim} does not exist; run an activation "
                "or `rf runtime install-agent` to provision the stable launcher"
            )
        argv = [str(self._shim), "--config", str(config_path), "start"]
        env = {key: os.environ[key] for key in _INHERITED if key in os.environ}
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
        return process_identity(record.pid) != record.process_identity
