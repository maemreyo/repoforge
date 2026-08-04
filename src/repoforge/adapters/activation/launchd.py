"""launchd LaunchAgent adapter that makes the RepoForge supervisor OS-resident.

The plist runs the *stable launcher* (``<release-root>/bin/rf``) in foreground -- which
starts the supervisor worker -- under ``KeepAlive``, so launchd relaunches it after a
crash and starts it at login. Because the launcher resolves through ``current``, the
agent always runs whichever release is active without the plist ever changing.
"""

from __future__ import annotations

import contextlib
import os
import plistlib
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ...domain.errors import ConfigError
from ...ports.activation import RestartOutcome
from ...ports.process_supervisor import RegistrarResult, RegistrarStatus

# The label is NOT defined here. It is supplied by the caller from the release store,
# which namespaces it per release root (``SUPERVISOR_AGENT_LABEL`` in domain.activation is
# the single base). A module-level default here would be a second source of truth that
# could drift from the production wiring while this module's own tests stayed green.


@dataclass(frozen=True, slots=True)
class LaunchAgentSpec:
    label: str
    launcher_path: Path
    config_path: Path
    stdout_path: Path
    stderr_path: Path
    inherited_env: dict[str, str]


def render_launch_agent(spec: LaunchAgentSpec) -> bytes:
    """Render the LaunchAgent plist. Pure: no filesystem or launchctl side effects."""
    # `launcher_path` is the supervisor shim, which execs the worker: launchd then owns
    # the supervisor process itself rather than a CLI wrapper whose child is the
    # supervisor (which would make the launchd pid differ from the runtime record pid).
    program_arguments = [
        str(spec.launcher_path),
        "--config",
        str(spec.config_path),
    ]
    payload: dict[str, object] = {
        "Label": spec.label,
        "ProgramArguments": program_arguments,
        "RunAtLoad": True,
        # Relaunch on crash but not in a tight loop after a clean stop.
        "KeepAlive": {"SuccessfulExit": False, "Crashed": True},
        "ProcessType": "Background",
        "StandardOutPath": str(spec.stdout_path),
        "StandardErrorPath": str(spec.stderr_path),
    }
    if spec.inherited_env:
        payload["EnvironmentVariables"] = dict(sorted(spec.inherited_env.items()))
    return plistlib.dumps(payload, sort_keys=True)


class CommandRunner(Protocol):
    def __call__(self, argv: list[str]) -> tuple[int, str]: ...


def _default_runner(argv: list[str]) -> tuple[int, str]:
    try:
        completed = subprocess.run(argv, capture_output=True, text=True, check=False, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc)
    return completed.returncode, (completed.stdout + completed.stderr).strip()


class LaunchdRegistrar:
    """Install/uninstall/inspect the supervisor LaunchAgent via ``launchctl``."""

    def __init__(
        self,
        *,
        spec: LaunchAgentSpec,
        agents_dir: Path,
        uid: int | None = None,
        runner: CommandRunner = _default_runner,
    ) -> None:
        self._spec = spec
        self._plist_path = agents_dir / f"{spec.label}.plist"
        self._uid = uid if uid is not None else os.getuid()
        self._run = runner
        # The definition launchd still has loaded while a staged permit sits on disk;
        # a failed bootstrap must restore THESE bytes (what launchd actually runs),
        # never the staged ones (which launchd never loaded).
        self._definition_before_stage: bytes | None = None

    @property
    def plist_path(self) -> Path:
        return self._plist_path

    def install(self) -> RegistrarResult:
        self._plist_path.parent.mkdir(parents=True, exist_ok=True)
        self._spec.stdout_path.parent.mkdir(parents=True, exist_ok=True)
        self._plist_path.write_bytes(render_launch_agent(self._spec))
        self._plist_path.chmod(0o644)
        # Replace any prior registration so the new plist is what runs.
        self._run(["launchctl", "bootout", self._domain(), str(self._plist_path)])
        code, detail = self._run(["launchctl", "bootstrap", self._domain(), str(self._plist_path)])
        if code != 0:
            raise ConfigError(
                f"LAUNCHD_BOOTSTRAP_FAILED: launchctl exited {code}: {detail or 'no output'}"
            )
        return RegistrarResult(
            status="installed",
            detail="LaunchAgent installed and bootstrapped; supervisor is now OS-resident.",
            unit_path=str(self._plist_path),
        )

    def uninstall(self) -> RegistrarResult:
        code, detail = self._run(["launchctl", "bootout", self._domain(), str(self._plist_path)])
        removed = False
        if self._plist_path.is_file():
            self._plist_path.unlink()
            removed = True
        return RegistrarResult(
            status="uninstalled" if removed or code == 0 else "not_registered",
            detail=detail or "LaunchAgent removed.",
            unit_path=str(self._plist_path),
        )

    def available(self) -> bool:
        """True when this supervisor is a registered, loaded launchd job."""
        return self.status().loaded

    def kickstart(self) -> RestartOutcome:
        """Stop-and-restart the registered job so launchd keeps owning the supervisor."""
        code, detail = self._run(
            ["launchctl", "kickstart", "-k", f"{self._domain()}/{self._spec.label}"]
        )
        if code != 0:
            return RestartOutcome(
                ok=False, detail=f"launchctl kickstart exited {code}: {detail or 'no output'}"
            )
        return RestartOutcome(ok=True, detail="kickstarted the launchd job")

    def stage_replacement_env(self, env: dict[str, str]) -> tuple[bool, str]:
        """Stage ``env`` into the job's plist ON DISK. NO launchctl side effects.

        Only the given keys are touched in ``EnvironmentVariables``; every other key,
        the file mode and the ownership are preserved, and the write is atomic. The
        loaded job is untouched -- launchd still runs the previous definition until
        ``bootstrap_replacement`` unloads and re-bootstraps it. This runs under the
        admission lock precisely because it cannot launch anything.
        """
        if not self._plist_path.is_file():
            return False, "LAUNCHD_PLIST_MISSING: no job plist to stage"
        try:
            previous = self._plist_path.read_bytes()
        except OSError as exc:
            return False, f"LAUNCHD_PLIST_UNREADABLE: {self._plist_path}: {exc}"
        try:
            payload = plistlib.loads(previous)
        except (plistlib.InvalidFileException, ValueError, TypeError) as exc:
            return False, f"LAUNCHD_PLIST_INVALID: {self._plist_path}: {exc}"
        if not isinstance(payload, dict):
            return False, "LAUNCHD_PLIST_INVALID: the plist root is not a dict"
        variables = payload.get("EnvironmentVariables")
        if variables is None:
            variables = {}
        elif not isinstance(variables, dict):
            return False, "LAUNCHD_PLIST_INVALID: EnvironmentVariables is not a dict"
        self._definition_before_stage = previous
        updated = {**payload, "EnvironmentVariables": {**variables, **env}}
        try:
            self._write_plist_atomic(updated, self._plist_format(previous))
        except (OSError, ConfigError) as exc:
            return False, f"LAUNCHD_PLIST_WRITE_FAILED: {exc}"
        return True, "replacement env staged into the job definition (not loaded)"

    def bootstrap_replacement(self) -> RestartOutcome:
        """Unload the loaded definition and bootstrap the STAGED plist.

        With ``RunAtLoad: True`` the bootstrap IS the replacement's single launch, so
        the caller must never follow this with ``kickstart``: a second launch would
        kill the just-started replacement and relaunch it against an already-consumed
        single-use permit. Every failure detail states the job state explicitly and
        never re-launches the previous definition on its own -- recovery belongs to
        the outer rollback protocol, which stages a fresh permit first.
        """
        if not self._plist_path.is_file():
            return RestartOutcome(
                ok=False, detail="LAUNCHD_PLIST_MISSING: no job plist to bootstrap"
            )
        try:
            previous = self._definition_before_stage or self._plist_path.read_bytes()
        except OSError as exc:
            return RestartOutcome(ok=False, detail=f"LAUNCHD_PLIST_UNREADABLE: {exc}")
        fmt = self._plist_format(previous)

        code, detail = self._run(["launchctl", "bootout", self._domain(), str(self._plist_path)])
        if code != 0:
            # The job was never unloaded, so launchd still runs the previous definition
            # (which never carried the staged permit). Restore the on-disk bytes to
            # match what is loaded; do NOT re-bootstrap, which would double-load it.
            restore = self._restore_plist(previous, fmt)
            return RestartOutcome(
                ok=False,
                detail=(
                    f"LAUNCHD_BOOTOUT_FAILED: launchctl exited {code}: {detail or 'no output'}; "
                    f"job state: LOADED (previous definition still loaded); "
                    f"previous job definition restored on disk: {restore}"
                ),
            )
        code, detail = self._run(["launchctl", "bootstrap", self._domain(), str(self._plist_path)])
        if code != 0:
            # The old definition is gone (bootout succeeded) and launchd rejected the
            # staged one. Restore the on-disk bytes ONLY: re-bootstrapping the old
            # definition here would launch a supervisor that cannot pass the CLOSING
            # worker-admission fence (it has no valid replacement permit) -- recovery
            # is the outer rollback protocol's job, with a fresh permit.
            restore = self._restore_plist(previous, fmt)
            return RestartOutcome(
                ok=False,
                detail=(
                    f"LAUNCHD_BOOTSTRAP_FAILED: launchctl exited {code}: {detail or 'no output'}; "
                    f"job state: UNLOADED; previous job definition restored on disk: {restore}"
                ),
            )
        return RestartOutcome(
            ok=True,
            detail="staged job definition bootstrapped; RunAtLoad launched the replacement",
        )

    def scrub_replacement_env(self, keys: tuple[str, ...]) -> tuple[bool, str]:
        """Remove ``keys`` from the plist on disk WITHOUT reloading the running job.

        The running replacement keeps its process environment -- harmless once the
        permit is consumed (``used=True``) and the epoch is open -- but the next
        launch reads the scrubbed definition, so the token stops being carried. A
        failed scrub is reported, never silently swallowed, and can never unstart the
        replacement.
        """
        if not keys:
            return True, ""
        if not self._plist_path.is_file():
            return True, "no job plist to scrub"
        try:
            previous = self._plist_path.read_bytes()
        except OSError as exc:
            return False, f"LAUNCHD_PLIST_UNREADABLE: {self._plist_path}: {exc}"
        try:
            payload = plistlib.loads(previous)
        except (plistlib.InvalidFileException, ValueError, TypeError) as exc:
            return False, f"LAUNCHD_PLIST_INVALID: {self._plist_path}: {exc}"
        if not isinstance(payload, dict):
            return False, "LAUNCHD_PLIST_INVALID: the plist root is not a dict"
        variables = payload.get("EnvironmentVariables")
        if not isinstance(variables, dict):
            return True, "no EnvironmentVariables to scrub"
        pruned = {key: value for key, value in variables.items() if key not in keys}
        if pruned == variables:
            return True, "no matching env keys to scrub"
        try:
            self._write_plist_atomic(
                {**payload, "EnvironmentVariables": pruned}, self._plist_format(previous)
            )
        except (OSError, ConfigError) as exc:
            return False, f"LAUNCHD_PLIST_WRITE_FAILED: {exc}"
        return True, "permit env scrubbed from the job definition; the running job was not reloaded"

    def _write_plist_atomic(self, payload: dict[str, object], fmt: plistlib.PlistFormat) -> None:
        """Atomically replace the plist, preserving the existing mode and ownership."""
        self._plist_path.parent.mkdir(parents=True, exist_ok=True)
        mode = self._plist_path.stat().st_mode & 0o777 if self._plist_path.is_file() else 0o644
        descriptor, tmp = tempfile.mkstemp(
            prefix=f".{self._plist_path.name}.", dir=str(self._plist_path.parent)
        )
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(plistlib.dumps(payload, fmt=fmt, sort_keys=True))
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(tmp, mode)
            if self._plist_path.is_file():
                stat = self._plist_path.stat()
                os.chown(tmp, stat.st_uid, stat.st_gid)
            os.replace(tmp, self._plist_path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise

    def _restore_plist(self, previous: bytes, fmt: plistlib.PlistFormat) -> str:
        """Restore the previous plist bytes on disk; NEVER re-bootstrap or launch."""
        try:
            payload = plistlib.loads(previous)
            self._write_plist_atomic(payload, fmt)
        except (OSError, ConfigError, plistlib.InvalidFileException, ValueError, TypeError) as exc:
            return f"restore FAILED to write the previous plist: {exc}"
        return "previous job definition restored on disk (no relaunch)"

    @staticmethod
    def _plist_format(data: bytes) -> plistlib.PlistFormat:
        return plistlib.FMT_BINARY if data.startswith(b"bplist") else plistlib.FMT_XML

    def status(self) -> RegistrarStatus:
        registered = self._plist_path.is_file()
        code, detail = self._run(["launchctl", "print", f"{self._domain()}/{self._spec.label}"])
        return RegistrarStatus(
            registered=registered,
            loaded=code == 0,
            detail="loaded" if code == 0 else (detail or "not loaded"),
            unit_path=str(self._plist_path),
        )

    def _domain(self) -> str:
        return f"gui/{self._uid}"
