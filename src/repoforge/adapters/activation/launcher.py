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

from ...domain.activation import AGENT_SECRET_FILE_ENV_VAR
from ...domain.errors import ConfigError
from ...domain.runtime import RuntimeRecord
from ..runtime.state_store import process_identity

# The shim re-enters the CLI, which re-derives the runtime environment from *its own*
# environment. Anything omitted here is silently lost across that hop -- a live sandbox
# activation failed exactly this way, with the worker reporting
# "Legacy configuration requires REPOFORGE_TUNNEL_ID" because the tunnel identity never
# reached it. Keep this in sync with SubprocessRuntimeLauncher's allowlist.
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
    "PYTHONPATH",
    "VIRTUAL_ENV",
    # Runtime identity the worker needs when the accepted configuration is local-only.
    "REPOFORGE_TUNNEL_ID",
    "REPOFORGE_TUNNEL_PROFILE",
    "REPOFORGE_CONFIG",
    "REPOFORGE_RELEASE_ROOT",
    # F-012: the handoff restarter's single-use replacement permit must survive the
    # shim hop, or the supervisor's first worker spawn is refused while CLOSING.
    "REPOFORGE_ADMISSION_PERMIT",
    # Under launchd there is no CONTROL_PLANE_API_KEY in the environment at all -- only
    # the path to the durable credential file. Dropping it here would leave a supervisor
    # restarted through this launcher with no credential and no way to obtain one.
    AGENT_SECRET_FILE_ENV_VAR,
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
        # `--background` matters: without it the CLI runs the supervisor in the
        # foreground as a *child* of this shim, so the published worker pid would not be
        # its own process-group leader and a later force-stop (killpg) would miss it.
        argv = [str(self._shim), "--config", str(config_path), "start", "--background"]
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
        """Terminate only the identity-bound supervisor process group.

        The recorded pid is validated against its start identity first, then its *actual*
        process group is resolved: assuming ``pgid == pid`` would silently signal nothing
        when the runtime was started as a child of another process.
        """
        if (
            record.pid is None
            or record.process_identity is None
            or process_identity(record.pid) != record.process_identity
        ):
            return False
        try:
            group = os.getpgid(record.pid)
        except (OSError, ProcessLookupError):
            # The process is already gone; nothing to terminate.
            return process_identity(record.pid) != record.process_identity
        if group != record.pid:
            # We validated ONE pid, not the group. If the recorded process is not its own
            # group leader, the group may contain unrelated processes, so signalling it
            # would exceed what we can prove we own. The runtime is started detached
            # (``--background``) precisely so it *is* the leader.
            return False
        for signal_number in (signal.SIGTERM, signal.SIGKILL):
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(group, signal_number)
            deadline = time.monotonic() + max(0.0, grace_seconds)
            while time.monotonic() < deadline:
                if process_identity(record.pid) != record.process_identity:
                    return True
                time.sleep(0.05)
        return process_identity(record.pid) != record.process_identity
