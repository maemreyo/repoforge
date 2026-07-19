"""Bounded, identity-safe process-tree inspection for timeout cleanup."""

from __future__ import annotations

import contextlib
import os
import selectors
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

_MAX_PROCESSES = 4_096
_MAX_PS_BYTES = 1_000_000
_PS_TIMEOUT_SECONDS = 2.0


@dataclass(frozen=True, slots=True)
class ProcessIdentity:
    pid: int
    ppid: int
    start_token: str


@dataclass(frozen=True, slots=True)
class DescendantSnapshot:
    identities: tuple[ProcessIdentity, ...]
    inspection_complete: bool
    diagnostic: str


def _parse_linux_stat(value: str) -> ProcessIdentity | None:
    """Parse `/proc/<pid>/stat` without assuming the command contains no `)`."""

    name_end = value.rfind(")")
    name_start = value.find("(")
    if name_start <= 0 or name_end <= name_start:
        return None
    try:
        pid = int(value[:name_start].strip())
        fields = value[name_end + 1 :].split()
        state = fields[0]
        ppid = int(fields[1])
        start_token = fields[19]
    except (IndexError, ValueError):
        return None
    if pid <= 0 or ppid < 0 or not start_token or state == "Z":
        return None
    return ProcessIdentity(pid=pid, ppid=ppid, start_token=start_token)


def _read_linux_identity(pid: int) -> ProcessIdentity | None:
    try:
        value = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    identity = _parse_linux_stat(value)
    return identity if identity is not None and identity.pid == pid else None


def _bounded_ps(argv: list[str]) -> str | None:
    """Read one `ps` response with byte and time bounds."""

    try:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return None
    if process.stdout is None:  # pragma: no cover - Popen contract
        return None
    selector = selectors.DefaultSelector()
    chunks: list[bytes] = []
    total = 0
    deadline = time.monotonic() + _PS_TIMEOUT_SECONDS
    try:
        selector.register(process.stdout, selectors.EVENT_READ)
        while total <= _MAX_PS_BYTES:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            events = selector.select(remaining)
            if not events:
                return None
            chunk = os.read(process.stdout.fileno(), min(65_536, _MAX_PS_BYTES + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        if total > _MAX_PS_BYTES:
            return None
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=max(0.01, deadline - time.monotonic()))
        if process.returncode not in {0, None}:
            return None
        return b"".join(chunks).decode("utf-8", errors="replace")
    except OSError:
        return None
    finally:
        selector.close()
        if process.poll() is None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                process.kill()
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=0.2)


def _parse_ps_line(line: str) -> ProcessIdentity | None:
    parts = line.strip().split(maxsplit=2)
    if len(parts) != 3:
        return None
    try:
        pid = int(parts[0])
        ppid = int(parts[1])
    except ValueError:
        return None
    if pid <= 0 or ppid < 0 or not parts[2]:
        return None
    return ProcessIdentity(pid=pid, ppid=ppid, start_token=parts[2])


def _read_ps_identities(pid: int | None = None) -> tuple[ProcessIdentity, ...] | None:
    argv = ["ps"]
    if pid is not None:
        argv.extend(["-p", str(pid)])
    argv.extend(["-o", "pid=,ppid=,lstart="])
    output = _bounded_ps(argv)
    if output is None:
        return None
    identities: list[ProcessIdentity] = []
    for line in output.splitlines():
        identity = _parse_ps_line(line)
        if identity is not None:
            identities.append(identity)
        if len(identities) > _MAX_PROCESSES:
            return None
    return tuple(identities)


def read_identity(pid: int) -> ProcessIdentity | None:
    if pid <= 0:
        return None
    if sys.platform.startswith("linux"):
        return _read_linux_identity(pid)
    identities = _read_ps_identities(pid)
    return (
        identities[0]
        if identities is not None and len(identities) == 1 and identities[0].pid == pid
        else None
    )


def _all_identities(limit: int) -> DescendantSnapshot:
    actual_limit = min(max(1, limit), _MAX_PROCESSES)
    if sys.platform.startswith("linux"):
        identities: list[ProcessIdentity] = []
        try:
            with os.scandir("/proc") as entries:
                for entry in entries:
                    if not entry.name.isdigit():
                        continue
                    identity = _read_linux_identity(int(entry.name))
                    if identity is not None:
                        identities.append(identity)
                    if len(identities) > actual_limit:
                        return DescendantSnapshot((), False, "process_limit_exceeded")
        except OSError:
            return DescendantSnapshot((), False, "proc_unavailable")
        return DescendantSnapshot(tuple(identities), True, "complete")
    identities = _read_ps_identities()
    if identities is None:
        return DescendantSnapshot((), False, "ps_probe_failed")
    if len(identities) > actual_limit:
        return DescendantSnapshot((), False, "process_limit_exceeded")
    return DescendantSnapshot(identities, True, "complete")


def inspect_descendants(root_pid: int, *, limit: int = _MAX_PROCESSES) -> DescendantSnapshot:
    if root_pid <= 0:
        return DescendantSnapshot((), False, "invalid_root_pid")
    inspected = _all_identities(limit)
    if not inspected.inspection_complete:
        return inspected
    actual_limit = min(max(1, limit), _MAX_PROCESSES)
    identities = inspected.identities
    children_of: dict[int, list[ProcessIdentity]] = {}
    for identity in identities:
        children_of.setdefault(identity.ppid, []).append(identity)
    descendants: list[ProcessIdentity] = []
    frontier = [root_pid]
    while frontier:
        parent = frontier.pop()
        for child in children_of.get(parent, []):
            descendants.append(child)
            if len(descendants) > actual_limit:
                return DescendantSnapshot((), False, "descendant_limit_exceeded")
            frontier.append(child.pid)
    return DescendantSnapshot(tuple(descendants), True, "complete")


def snapshot_descendants(
    root_pid: int, *, limit: int = _MAX_PROCESSES
) -> tuple[ProcessIdentity, ...]:
    return inspect_descendants(root_pid, limit=limit).identities


def identity_is_current(identity: ProcessIdentity) -> bool:
    current = read_identity(identity.pid)
    return (
        current is not None
        and current.pid == identity.pid
        and current.start_token == identity.start_token
    )


def _pidfd_open(pid: int) -> int | None:
    opener = getattr(os, "pidfd_open", None)
    if opener is None:
        return None
    try:
        return int(opener(pid, 0))
    except OSError:
        return None


def _pidfd_send_signal(fd: int, sig: int) -> bool:
    sender = getattr(signal, "pidfd_send_signal", None)
    if sender is None:
        return False
    try:
        sender(fd, sig, None, 0)
    except OSError:
        return False
    return True


def atomic_process_signalling_available() -> bool:
    """Return whether this host can bind and signal one exact process object."""

    if not sys.platform.startswith("linux"):
        return False
    if not callable(getattr(os, "pidfd_open", None)) or not callable(
        getattr(signal, "pidfd_send_signal", None)
    ):
        return False
    fd = _pidfd_open(os.getpid())
    if fd is None:
        return False
    with contextlib.suppress(OSError):
        os.close(fd)
    return True


def kill_identity(identity: ProcessIdentity, sig: int = signal.SIGKILL) -> bool:
    """Atomically signal the captured process, or skip when that is unavailable.

    Opening a Linux pidfd binds the kernel process object. Identity is re-read
    after the handle is open, so a PID reused before the open is rejected and a
    PID reused afterward cannot redirect the signal. Hosts without an atomic
    process handle fail closed instead of using a racy check-then-``kill(pid)``.
    """

    fd = _pidfd_open(identity.pid)
    if fd is None:
        return False
    try:
        if not identity_is_current(identity):
            return False
        return _pidfd_send_signal(fd, sig)
    finally:
        with contextlib.suppress(OSError):
            os.close(fd)
