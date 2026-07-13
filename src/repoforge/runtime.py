"""Persisted, local-only runtime generation state."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .errors import ConfigError


@dataclass(frozen=True)
class RuntimeState:
    """The generation currently loaded by a live local MCP process."""

    pid: int
    active_generation: int
    started_at: str


@dataclass(frozen=True)
class ManagedRuntime:
    """The tunnel-client process group managed for one reviewed generation."""

    pid: int
    active_generation: int
    profile: str
    executable: str
    started_at: str


def read_runtime_state(path: Path) -> RuntimeState | None:
    """Return a valid live runtime record, ignoring a stale process record."""
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
    if (
        not isinstance(pid, int)
        or isinstance(pid, bool)
        or pid <= 0
        or not isinstance(generation, int)
        or isinstance(generation, bool)
        or generation <= 0
        or not isinstance(started_at, str)
    ):
        raise ConfigError(f"Runtime state {path} is invalid")
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        path.unlink(missing_ok=True)
        return None
    except PermissionError:
        pass
    return RuntimeState(pid=pid, active_generation=generation, started_at=started_at)


def write_runtime_state(path: Path, generation: int) -> RuntimeState:
    """Atomically record the generation loaded by the current local process."""
    if generation <= 0:
        raise ConfigError("Runtime generation must be positive")
    state = RuntimeState(
        pid=os.getpid(),
        active_generation=generation,
        started_at=datetime.now(timezone.utc).isoformat(),
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
        completed = subprocess.run(
            ["ps", "-o", "command=", "-p", str(pid)],
            capture_output=True,
            check=False,
            text=True,
            timeout=5,
        )
        process_group = os.getpgid(pid)
    except (OSError, subprocess.SubprocessError, ProcessLookupError):
        path.unlink(missing_ok=True)
        return None
    command = completed.stdout.strip()
    if completed.returncode != 0 or process_group != pid or not command.startswith(executable):
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
