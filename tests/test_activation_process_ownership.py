"""Round-3 finding 2: force-stop must terminate the runtime's REAL process group.

These spawn actual processes. The bug being pinned: the release-aware launcher started
the CLI in the foreground, so the published worker pid was not its own process-group
leader, and a ``killpg(record.pid)`` therefore signalled a group that did not exist.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import time

import pytest

from repoforge.adapters.activation.launcher import ReleaseAwareRuntimeLauncher
from repoforge.adapters.runtime.state_store import process_identity
from repoforge.domain.runtime import RuntimePhase, RuntimeRecord

_SURFACE = "c" * 64


def _record(pid: int, identity: str) -> RuntimeRecord:
    return RuntimeRecord(
        protocol_version=1,
        phase=RuntimePhase.HEALTHY,
        pid=pid,
        process_identity=identity,
        active_generation=1,
        accepted_generation=1,
        tunnel_profile="p",
        tunnel_profile_fingerprint=_SURFACE,
        tool_surface_hash=_SURFACE,
        started_at="2026-07-25T00:00:00+00:00",
        updated_at="2026-07-25T00:00:00+00:00",
        correlation_id="c" * 24,
        child_pid=pid,
        child_process_identity=identity,
    )


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


def _await_gone(pid: int, *, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.05)
    return not _alive(pid)


@contextlib.contextmanager
def _process_group_with_a_child():
    """Start a detached group: a leader shell plus a child, mirroring runtime + worker.

    Yields ``(leader_popen, child_pid)``. The leader is this test's direct child, so its
    exit must be observed with ``Popen.poll()`` -- a killed-but-unreaped process is a
    zombie and still answers ``kill(pid, 0)``.
    """
    leader = subprocess.Popen(
        ["/bin/sh", "-c", 'sleep 120 & echo "$!"; wait'],
        stdout=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        assert leader.stdout is not None
        child_pid = int(leader.stdout.readline().strip())
        yield leader, child_pid
    finally:
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            os.killpg(os.getpgid(leader.pid), signal.SIGKILL)
        with contextlib.suppress(Exception):
            leader.wait(timeout=5)


def _await_leader_gone(leader: subprocess.Popen[str], *, timeout: float = 5.0) -> bool:
    """Reap while waiting: poll() both observes and clears the zombie."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if leader.poll() is not None:
            return True
        time.sleep(0.05)
    return leader.poll() is not None


def test_force_stop_kills_the_whole_process_group(tmp_path) -> None:
    launcher = ReleaseAwareRuntimeLauncher(tmp_path / "bin" / "rf")
    with _process_group_with_a_child() as (leader, child_pid):
        identity = process_identity(leader.pid)
        assert identity is not None

        assert launcher.force_stop(_record(leader.pid, identity), grace_seconds=2.0) is True

        assert _await_leader_gone(leader), "the runtime process must be gone"
        assert _await_gone(child_pid), "no descendant may survive a force stop"


def test_force_stop_refuses_a_group_it_cannot_prove_it_owns(tmp_path) -> None:
    """Round-4 finding 4: validating one pid does not authorize killing its whole group.

    When the recorded process is not its own group leader, the group may contain
    unrelated processes, so force-stop must decline rather than widen its blast radius.
    The runtime is started detached (``--background``) precisely so it IS the leader.
    """
    launcher = ReleaseAwareRuntimeLauncher(tmp_path / "bin" / "rf")
    with _process_group_with_a_child() as (leader, child_pid):
        identity = process_identity(child_pid)
        if identity is None:  # pragma: no cover - platform without ps lstart
            pytest.skip("host cannot report a process start identity")
        assert os.getpgid(child_pid) != child_pid, "precondition: child is not the leader"

        assert launcher.force_stop(_record(child_pid, identity), grace_seconds=0.2) is False
        # Nothing was signalled: neither the unverified group nor its leader.
        assert _alive(child_pid)
        assert leader.poll() is None


def test_force_stop_refuses_a_pid_whose_identity_does_not_match(tmp_path) -> None:
    """PID reuse guard: never signal a process that is not the recorded one."""
    launcher = ReleaseAwareRuntimeLauncher(tmp_path / "bin" / "rf")
    with _process_group_with_a_child() as (leader, _child_pid):
        # A deliberately wrong identity for a live pid.
        assert launcher.force_stop(_record(leader.pid, "d" * 64), grace_seconds=0.2) is False
        assert leader.poll() is None, "a mismatched identity must never be signalled"


def test_launcher_starts_the_runtime_detached_in_the_background(tmp_path) -> None:
    """`--background` is required so the worker owns its own process group."""
    shim = tmp_path / "bin" / "rf"
    shim.parent.mkdir(parents=True)
    marker = tmp_path / "argv.txt"
    shim.write_text(
        '#!/bin/sh\nprintf "%s\\n" "$@" > ' + str(marker) + "\n",
        encoding="utf-8",
    )
    shim.chmod(0o755)

    ReleaseAwareRuntimeLauncher(shim).start(tmp_path / "config.toml", foreground=True, extra_env={})

    recorded = marker.read_text(encoding="utf-8").split()
    assert "start" in recorded
    assert "--background" in recorded
