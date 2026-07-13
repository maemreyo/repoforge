from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from repoforge.adapters.runtime.local_runtime import (
    clear_runtime_state,
    managed_start_claim,
    read_managed_runtime,
    read_runtime_log,
    read_runtime_state,
    stop_managed_runtime,
    write_managed_runtime,
    write_runtime_state,
)
from repoforge.domain.errors import ConfigError


def test_runtime_state_records_the_current_process_and_generation(tmp_path: Path) -> None:
    # Given: no active runtime state.
    state_path = tmp_path / "runtime.json"

    # When: the MCP process records its loaded generation.
    state = write_runtime_state(state_path, 3, "surface")

    # Then: status reports the same live process and generation.
    assert state.pid == os.getpid()
    assert state.process_identity
    assert read_runtime_state(state_path) == state
    assert state.tool_surface_hash == "surface"


def test_runtime_state_ignores_and_removes_a_dead_process_record(tmp_path: Path) -> None:
    # Given: a state file for a process that cannot exist.
    state_path = tmp_path / "runtime.json"
    state_path.write_text(
        json.dumps(
            {
                "pid": 999_999,
                "active_generation": 2,
                "started_at": "now",
                "process_identity": "0" * 64,
            }
        ),
        encoding="utf-8",
    )

    # When: runtime status is read.
    state = read_runtime_state(state_path)

    # Then: it is considered stopped and the stale record is removed.
    assert state is None
    assert not state_path.exists()


def test_runtime_state_rejects_a_reused_pid_with_another_identity(tmp_path: Path) -> None:
    # Given: a live PID is paired with identity facts from another process instance.
    state_path = tmp_path / "runtime.json"
    state_path.write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "active_generation": 2,
                "started_at": "now",
                "process_identity": "0" * 64,
            }
        ),
        encoding="utf-8",
    )

    # When: runtime status validates the persisted process identity.
    state = read_runtime_state(state_path)

    # Then: PID reuse or forged state cannot impersonate the MCP runtime.
    assert state is None
    assert not state_path.exists()


def test_runtime_state_discards_legacy_pid_only_record(tmp_path: Path) -> None:
    # Given: a live state record written before process identity was persisted.
    state_path = tmp_path / "runtime.json"
    state_path.write_text(
        json.dumps({"pid": os.getpid(), "active_generation": 2, "started_at": "now"}),
        encoding="utf-8",
    )

    # When: the upgraded runtime reads the PID-only record.
    state = read_runtime_state(state_path)

    # Then: it fails closed until the live process republishes identity-bound state.
    assert state is None
    assert not state_path.exists()


def test_runtime_state_cleanup_preserves_a_replacement_record(tmp_path: Path) -> None:
    # Given: a current runtime record.
    state_path = tmp_path / "runtime.json"
    write_runtime_state(state_path, 4)

    # When: another process attempts cleanup.
    clear_runtime_state(state_path, os.getpid() + 1)

    # Then: the live record remains available.
    assert read_runtime_state(state_path) is not None


def test_managed_runtime_stops_only_the_recorded_process_group(tmp_path: Path) -> None:
    # Given: a child process owns its own process group.
    state_path = tmp_path / "managed-runtime.json"
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True
    )
    write_managed_runtime(
        state_path,
        pid=process.pid,
        generation=2,
        profile="repoforge",
        executable=sys.executable,
    )

    # When: the managed runtime is stopped.
    stopped = stop_managed_runtime(state_path)
    process.wait(timeout=5)

    # Then: only the recorded child is stopped and its record is removed.
    assert stopped is not None
    assert stopped.pid == process.pid
    assert read_managed_runtime(state_path) is None


def test_runtime_log_returns_bounded_redacted_tail(tmp_path: Path) -> None:
    # Given: a supervisor-owned log containing a credential-shaped value.
    log_path = tmp_path / "managed-runtime.log"
    log_path.write_text("first\ntoken=abc123\nlast\n", encoding="utf-8")

    # When: the bounded tail is requested.
    lines = read_runtime_log(log_path, 2)

    # Then: only requested lines are returned and secrets are redacted.
    assert lines == ["token=<redacted>", "last"]


def test_managed_start_claim_rejects_concurrent_claim(tmp_path: Path) -> None:
    # Given: another process holds the startup lock.
    claim_path = tmp_path / "managed-runtime.start.lock"
    with claim_path.open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)

        # When: a second runtime launch attempts to claim the same lock.
        with pytest.raises(ConfigError, match="ALREADY_STARTING"), managed_start_claim(claim_path):
            pass

    # Then: a later start can acquire the released claim.
    with managed_start_claim(claim_path):
        pass
