"""Persisted, local-only runtime generation state."""

from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ...domain.errors import ConfigError
from ...domain.redaction import redact_text

_LOG_READ_LIMIT = 1_000_000
_PROCESS_IDENTITY = re.compile(r"^[a-f0-9]{64}$")
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
    process_identity: str = ""


@dataclass(frozen=True)
class ManagedRuntime:
    """The tunnel-client process group managed for one reviewed generation."""

    pid: int
    active_generation: int
    profile: str
    executable: str
    started_at: str
    process_identity: str = ""


def _read_process_identity(pid: int) -> str | None:
    """Hash stable local process facts so a reused PID cannot impersonate the MCP runtime."""
    try:
        completed = subprocess.run(
            ["ps", "-o", "lstart=", "-o", "command=", "-p", str(pid)],
            capture_output=True,
            check=False,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    facts = completed.stdout.strip()
    if completed.returncode != 0 or not facts:
        return None
    return hashlib.sha256(facts.encode("utf-8")).hexdigest()


def read_runtime_state(path: Path) -> RuntimeState | None:
    """Return a live identity-validated runtime record, cleaning stale state."""
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
    process_identity = raw.get("process_identity", "")
    if process_identity == "":
        # PID-only records cannot distinguish a live process from a reused PID.
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
        or not isinstance(process_identity, str)
        or not _PROCESS_IDENTITY.fullmatch(process_identity)
    ):
        raise ConfigError(f"Runtime state {path} is invalid")
    if _read_process_identity(pid) != process_identity:
        path.unlink(missing_ok=True)
        return None
    return RuntimeState(
        pid=pid,
        active_generation=generation,
        started_at=started_at,
        tool_surface_hash=tool_surface_hash,
        process_identity=process_identity,
    )


def write_runtime_state(path: Path, generation: int, tool_surface_hash: str = "") -> RuntimeState:
    """Atomically record the generation and identity loaded by the current process."""
    if generation <= 0:
        raise ConfigError("Runtime generation must be positive")
    process_identity = _read_process_identity(os.getpid())
    if process_identity is None:
        raise ConfigError("Cannot determine the current runtime process identity")
    state = RuntimeState(
        pid=os.getpid(),
        active_generation=generation,
        started_at=datetime.now(timezone.utc).isoformat(),
        tool_surface_hash=tool_surface_hash,
        process_identity=process_identity,
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
    identity = raw.get("process_identity", "")
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
        or not isinstance(identity, str)
        or not _PROCESS_IDENTITY.fullmatch(identity)
    ):
        path.unlink(missing_ok=True)
        return None
    try:
        process_group = os.getpgid(pid)
    except (OSError, ProcessLookupError):
        path.unlink(missing_ok=True)
        return None
    if process_group != pid or _read_process_identity(pid) != identity:
        path.unlink(missing_ok=True)
        return None
    return ManagedRuntime(pid, generation, profile, executable, started_at, identity)


def write_managed_runtime(
    path: Path, *, pid: int, generation: int, profile: str, executable: str
) -> ManagedRuntime:
    """Persist an identity-bound tunnel process group created by this command."""
    if pid <= 0 or generation <= 0 or not profile or not executable:
        raise ConfigError("Managed runtime state requires valid process and generation fields")
    identity = _read_process_identity(pid)
    if identity is None:
        raise ConfigError("Cannot determine managed runtime process identity")
    runtime = ManagedRuntime(
        pid=pid,
        active_generation=generation,
        profile=profile,
        executable=executable,
        started_at=datetime.now(timezone.utc).isoformat(),
        process_identity=identity,
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


def runtime_log_files(path: Path) -> tuple[Path, ...]:
    """Return numeric rotations followed by the active log in chronological order."""
    prefix = path.name + "."
    rotations: list[tuple[int, Path]] = []
    if path.parent.is_dir():
        for candidate in path.parent.glob(prefix + "*"):
            suffix = candidate.name[len(prefix) :]
            if suffix.isdigit() and candidate.is_file():
                rotations.append((int(suffix), candidate))
    ordered = [candidate for _, candidate in sorted(rotations, reverse=True)]
    if path.is_file():
        ordered.append(path)
    return tuple(ordered)


def read_runtime_log(path: Path, tail: int) -> list[str]:
    """Read one global bounded redacted tail across active and rotated runtime logs."""
    if tail <= 0 or tail > 1_000:
        raise ConfigError("Runtime log tail must be between 1 and 1000 lines")
    files = runtime_log_files(path)
    if not files:
        return []
    remaining = _LOG_READ_LIMIT
    chunks: list[str] = []
    try:
        for candidate in reversed(files):
            if remaining <= 0:
                break
            with candidate.open("rb") as handle:
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                start = max(0, size - remaining)
                handle.seek(start)
                data = handle.read(remaining)
            text = data.decode("utf-8", errors="replace")
            if start > 0 and "\n" in text:
                text = text.split("\n", 1)[1]
            chunks.insert(0, text)
            remaining -= len(data)
    except OSError as exc:
        raise ConfigError(f"Cannot read runtime log {path.name}: {exc}") from exc
    text = "".join(chunks)
    return [redact_text(line) for line in text.splitlines()[-tail:]]
