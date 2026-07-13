"""Persisted, local-only runtime generation state."""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .errors import ConfigError

_LOG_READ_LIMIT = 1_000_000
_SECRET_VALUE = re.compile(
    r"(?i)\b(control_plane_api_key|authorization|token|secret|password)\b(\s*[:=]\s*)([^\s]+)"
)


@dataclass(frozen=True)
class RuntimeState:
    """The generation currently loaded by a live local MCP process."""

    pid: int
    active_generation: int
    started_at: str
    tool_surface_hash: str = ""
    executable: str = ""


@dataclass(frozen=True)
class ManagedRuntime:
    """The tunnel-client process group managed for one reviewed generation."""

    pid: int
    active_generation: int
    profile: str
    executable: str
    started_at: str


def _read_process_command(pid: int) -> str | None:
    """Return the bounded process command used to validate a persisted PID."""
    try:
        completed = subprocess.run(
            ["ps", "-o", "command=", "-p", str(pid)],
            capture_output=True,
            check=False,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    command = completed.stdout.strip()
    if completed.returncode != 0 or not command:
        return None
    return command


def _process_matches_executable(pid: int, executable: str) -> bool:
    """Reject dead, reused, or forged PIDs that do not match the recorded executable."""
    command = _read_process_command(pid)
    if command is None:
        return False
    return command == executable or command.startswith(f"{executable} ")


def read_runtime_state(path: Path) -> RuntimeState | None:
    """Return a live identity-validated MCP runtime record, cleaning stale state."""
    if not path.is_file():
        return None
    try:
        raw: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConfigError(f"Cannot read runtime state {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"Runtime state {path} must be a JSON object")
    pid = raw.get("pid")
    generation = raw.get("active_generation")
    started_at = raw.get("started_at")
    tool_surface_hash = raw.get("tool_surface_hash", "")
    executable = raw.get("executable", "")
    if executable == "":
        # State written before executable identity support cannot safely distinguish PID reuse.
        path.unlink(missing_ok=True)
        return None
    if (
        not isinstance(pid, int)
        or isinstance(pid, bool)
        or pid <= 0
        or not isinstance(generation, int)
        or isinstance(generation, bool)
        or generation <= 0
        or not isinstance(started_at, str)
        or not isinstance(tool_surface_hash, str)
        or not isinstance(executable, str)
    ):
        raise ConfigError(f"Runtime state {path} is invalid")
    if not _process_matches_executable(pid, executable):
        path.unlink(missing_ok=True)
        return None
    return RuntimeState(
        pid=pid,
        active_generation=generation,
        started_at=started_at,
        tool_surface_hash=tool_surface_hash,
        executable=executable,
    )


def write_runtime_state(path: Path, generation: int, tool_surface_hash: str = "") -> RuntimeState:
    """Atomically record the generation and identity loaded by the current local process."""
    if generation <= 0:
        raise ConfigError("Runtime generation must be positive")
    state = RuntimeState(
        pid=os.getpid(),
        active_generation=generation,
        started_at=datetime.now(timezone.utc).isoformat(),
        tool_surface_hash=tool_surface_hash,
        executable=sys.executable,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write(json.dumps(asdict(state), sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return state


def clear_runtime_state(path: Path, pid: int) -> None:
    """Remove this process's state record without deleting a replacement's record."""
    state = read_runtime_state(path)
    if state is not None and state.pid == pid:
        path.unlink(missing_ok=True)


def read_managed_runtime(path: Path) -> ManagedRuntime | None:
    """Return the validated managed tunnel process, cleaning stale state."""
    if not path.is_file():
        return None
    try:
        raw: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConfigError(f"Cannot read managed runtime state {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"Managed runtime state {path} must be a JSON object")
    pid = raw.get("pid")
    generation = raw.get("active_generation")
    profile = raw.get("profile")
    executable = raw.get("executable")
    started_at = raw.get("started_at")
    if (
        not isinstance(pid, int)
        or isinstance(pid, bool)
        or pid <= 0
        or not isinstance(generation, int)
        or isinstance(generation, bool)
        or generation <= 0
        or not isinstance(profile, str)
        or not profile
        or not isinstance(executable, str)
        or not executable
        or not isinstance(started_at, str)
    ):
        raise ConfigError(f"Managed runtime state {path} is invalid")
    try:
        process_group = os.getpgid(pid)
    except (OSError, ProcessLookupError):
        path.unlink(missing_ok=True)
        return None
    if process_group != pid or not _process_matches_executable(pid, executable):
        path.unlink(missing_ok=True)
        return None
    return ManagedRuntime(pid, generation, profile, executable, started_at)


def write_managed_runtime(
    path: Path, *, pid: int, generation: int, profile: str, executable: str
) -> ManagedRuntime:
    """Persist an identity-bound tunnel process group created by this command."""
    if pid <= 0 or generation <= 0 or not profile or not executable:
        raise ConfigError("Managed runtime state requires valid process and generation fields")
    runtime = ManagedRuntime(
        pid=pid,
        active_generation=generation,
        profile=profile,
        executable=executable,
        started_at=datetime.now(timezone.utc).isoformat(),
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write(json.dumps(asdict(runtime), sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return runtime


def stop_managed_runtime(path: Path, timeout_seconds: int = 15) -> ManagedRuntime | None:
    """Terminate only the identity-validated tunnel process group recorded at path."""
    runtime = read_managed_runtime(path)
    if runtime is None:
        return None
    os.killpg(runtime.pid, signal.SIGTERM)
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if read_managed_runtime(path) is None:
            return runtime
        time.sleep(0.1)
    os.killpg(runtime.pid, signal.SIGKILL)
    path.unlink(missing_ok=True)
    return runtime


@contextmanager
def managed_start_claim(path: Path) -> Iterator[None]:
    """Hold an exclusive non-blocking local claim while creating a managed child."""
    import fcntl

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ConfigError("ALREADY_STARTING: another runtime start is in progress") from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def read_runtime_log(path: Path, tail: int) -> list[str]:
    """Read a bounded redacted tail from a supervisor-owned local log file."""
    if tail <= 0 or tail > 1_000:
        raise ConfigError("Runtime log tail must be between 1 and 1000 lines")
    if not path.is_file():
        return []
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - _LOG_READ_LIMIT))
            text = handle.read().decode("utf-8", errors="replace")
    except OSError as exc:
        raise ConfigError(f"Cannot read runtime log {path}: {exc}") from exc
    return [_SECRET_VALUE.sub(r"\1\2<redacted>", line) for line in text.splitlines()[-tail:]]
