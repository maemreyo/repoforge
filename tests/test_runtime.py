from __future__ import annotations

import json
import os
from pathlib import Path

from repoforge.runtime import clear_runtime_state, read_runtime_state, write_runtime_state


def test_runtime_state_records_the_current_process_and_generation(tmp_path: Path) -> None:
    # Given: no active runtime state.
    state_path = tmp_path / "runtime.json"

    # When: the MCP process records its loaded generation.
    state = write_runtime_state(state_path, 3)

    # Then: status reports the same live process and generation.
    assert state.pid == os.getpid()
    assert read_runtime_state(state_path) == state


def test_runtime_state_ignores_and_removes_a_dead_process_record(tmp_path: Path) -> None:
    # Given: a state file for a process that cannot exist.
    state_path = tmp_path / "runtime.json"
    state_path.write_text(
        json.dumps({"pid": 999_999, "active_generation": 2, "started_at": "now"}),
        encoding="utf-8",
    )

    # When: runtime status is read.
    state = read_runtime_state(state_path)

    # Then: it is considered stopped and the stale record is removed.
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
