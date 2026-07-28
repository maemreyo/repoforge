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
from repoforge.domain.runtime_events import (
    RuntimeEventV1,
    encode_runtime_event,
    parse_runtime_event,
)


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
        [
            sys.executable,
            "-c",
            "import time; print('ready', flush=True); time.sleep(60)",
        ],
        start_new_session=True,
        stdout=subprocess.PIPE,
        text=True,
    )
    assert process.stdout is not None
    assert process.stdout.readline().strip() == "ready"
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


def test_runtime_log_merges_rotations_in_chronological_order(tmp_path: Path) -> None:
    log_path = tmp_path / "managed-runtime.log"
    log_path.with_suffix(".log.3").write_text("oldest\n", encoding="utf-8")
    log_path.with_suffix(".log.2").write_text("older\n", encoding="utf-8")
    log_path.with_suffix(".log.1").write_text("previous\n", encoding="utf-8")
    log_path.write_text("current-1\ncurrent-2\n", encoding="utf-8")

    assert read_runtime_log(log_path, 4) == ["older", "previous", "current-1", "current-2"]


def test_runtime_event_parser_never_fabricates_timestamp() -> None:
    parsed = parse_runtime_event("legacy plaintext")

    assert parsed.timestamp is None
    assert parsed.timestamp_state == "unavailable"
    assert parsed.parse_state == "legacy_plaintext"
    assert parsed.message == "legacy plaintext"


def test_runtime_event_v1_round_trip_preserves_observed_fields() -> None:
    event = RuntimeEventV1(
        observed_at="2026-07-21T12:00:00+00:00",
        component="tunnel_client",
        stream="stdout",
        level="INFO",
        event_kind="process_output",
        message="ready",
        correlation_id="corr-1",
        operation_id="op-1",
        receipt_id=None,
        trace_id=None,
        workspace_hash=None,
        repository_hash=None,
    )

    parsed = parse_runtime_event(encode_runtime_event(event))

    assert parsed.parse_state == "structured_v1"
    assert parsed.timestamp == event.observed_at
    assert parsed.timestamp_state == "observed"
    assert parsed.component == "tunnel_client"
    assert parsed.stream == "stdout"
    assert parsed.event_kind == "process_output"
    assert parsed.message == "ready"
    assert parsed.correlation_id == "corr-1"
    assert parsed.operation_id == "op-1"
