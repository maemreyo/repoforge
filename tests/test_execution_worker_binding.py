"""Durable execution-worker bindings and process classification (#368).

An execution worker spawned by a supervisor that later dies is invisible to the next
supervisor: children live only in the parent's RAM. A per-worker durable binding
(pid, start token, pgid, generation, release sha, owner supervisor identity) plus
exact-entry-point classification is what lets a later process reclaim orphans without
killing unrelated processes from the same release.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from repoforge.adapters.persistence.json_execution_worker_binding_store import (
    JsonExecutionWorkerBindingStore,
)
from repoforge.adapters.subprocess.process_tree import read_command_line
from repoforge.domain.execution_worker import (
    ExecutionWorkerBinding,
    execution_worker_binding_from_payload,
    execution_worker_binding_payload,
    is_execution_worker_entry_point,
    validate_execution_worker_binding,
)
from repoforge.testing import InMemoryLockManager

_WORKER_ID = "worker-0123456789ab"
_SHA = "a" * 64
_RELEASE = "0123abc"


def _binding(**overrides: object) -> ExecutionWorkerBinding:
    base: dict[str, object] = {
        "worker_id": _WORKER_ID,
        "pid": 4242,
        "pgid": 4242,
        "process_start_token": "2026-07-29 09:26:21 +0000",
        "generation": 12,
        "release_sha": _RELEASE,
        "supervisor_pid": 4241,
        "supervisor_process_identity": _SHA,
        "correlation_id": "c" * 24,
        "started_at": "2026-07-29T09:26:21+00:00",
        "state": "running",
    }
    base.update(overrides)
    return ExecutionWorkerBinding(**base)


def test_binding_round_trips_through_a_payload() -> None:
    binding = _binding()
    assert (
        execution_worker_binding_from_payload(execution_worker_binding_payload(binding)) == binding
    )


def test_binding_requires_a_positive_pid_and_matching_pgid() -> None:
    with pytest.raises(ValueError, match="pid"):
        validate_execution_worker_binding(_binding(pid=0))
    with pytest.raises(ValueError, match="pgid"):
        validate_execution_worker_binding(_binding(pgid=999))


def test_binding_requires_a_known_state() -> None:
    with pytest.raises(ValueError, match="state"):
        validate_execution_worker_binding(_binding(state="mystery"))


def test_binding_validates_the_owner_supervisor_identity() -> None:
    with pytest.raises(ValueError, match="supervisor_process_identity"):
        validate_execution_worker_binding(_binding(supervisor_process_identity="not-a-digest"))


def test_binding_accepts_a_legacy_missing_release_sha() -> None:
    payload = execution_worker_binding_payload(_binding())
    del payload["release_sha"]
    binding = execution_worker_binding_from_payload(dict(payload))
    assert binding.release_sha is None


def test_store_round_trips_and_lists_bindings(tmp_path: Path) -> None:
    store = JsonExecutionWorkerBindingStore(tmp_path / "state", InMemoryLockManager())
    binding = _binding()
    store.put(binding)
    assert store.get(_WORKER_ID) == binding
    assert store.list_all() == (binding,)


def test_store_updates_state_with_cas(tmp_path: Path) -> None:
    store = JsonExecutionWorkerBindingStore(tmp_path / "state", InMemoryLockManager())
    store.put(_binding())
    updated = store.update_state(_WORKER_ID, "reclaimed")
    assert updated is not None and updated.state == "reclaimed"
    assert store.get(_WORKER_ID) is not None
    assert store.get(_WORKER_ID).state == "reclaimed"


def test_store_rejects_an_unknown_worker_id(tmp_path: Path) -> None:
    store = JsonExecutionWorkerBindingStore(tmp_path / "state", InMemoryLockManager())
    assert store.get("worker-ffffffffffff") is None
    with pytest.raises(ValueError, match="worker_id"):
        store.put(_binding(worker_id="not-a-worker"))


def test_validate_rejects_a_running_binding_without_a_start_token() -> None:
    """A modern `running` binding must carry the process start token (#420)."""
    with pytest.raises(ValueError, match="process_start_token"):
        validate_execution_worker_binding(_binding(process_start_token=None))


def test_a_legacy_running_payload_without_a_token_decodes_as_legacy_unproven() -> None:
    """Pre-token records read back as an unproven concern, never a valid running one."""
    payload = execution_worker_binding_payload(_binding())
    payload["process_start_token"] = None
    binding = execution_worker_binding_from_payload(dict(payload))
    assert binding.state == "legacy_unproven"
    assert binding.process_start_token is None


def test_store_surfaces_an_unreadable_record_in_list_page(tmp_path: Path) -> None:
    """An unreadable registry record is reported by id, not silently dropped (#420)."""
    store = JsonExecutionWorkerBindingStore(tmp_path / "state", InMemoryLockManager())
    store.put(_binding())
    (store.root / "worker-999999999999.json").write_text("{corrupt", encoding="utf-8")

    page = store.list_page()

    assert page.scan_complete is True
    assert page.unreadable_ids == ("worker-999999999999",)
    assert tuple(item.worker_id for item in page.records) == (_WORKER_ID,)


def test_classification_requires_the_exact_entry_point() -> None:
    assert (
        is_execution_worker_entry_point(
            (
                "/opt/repoforge/venv/bin/python",
                "-m",
                "repoforge.interfaces.runtime.execution_worker",
                "--config",
                "/home/dev/config.toml",
                "--generation",
                "12",
            )
        )
        is True
    )
    assert (
        is_execution_worker_entry_point(
            (
                "/opt/repoforge/venv/bin/python",
                "-m",
                "repoforge.interfaces.runtime.supervisor",
                "--config",
                "/home/dev/config.toml",
            )
        )
        is False
    )
    assert (
        is_execution_worker_entry_point(
            ("/opt/repoforge/venv/bin/python", "-c", "import repoforge")
        )
        is False
    )
    assert is_execution_worker_entry_point(()) is False


def test_command_line_reader_returns_the_live_process_argv() -> None:
    import os
    import subprocess
    import sys
    import time

    script = "import time; time.sleep(30)"
    process = subprocess.Popen(
        [sys.executable, "-c", script],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 10
        argv = None
        while time.monotonic() < deadline:
            candidate = read_command_line(process.pid)
            # On macOS a venv's sys.executable is a shim that re-execs onto the
            # real interpreter, so `ps -o command=` can report a transient argv
            # during that window (same race as the worker identity settle, #420).
            # Poll until the post-exec argv -- whose basename is python -- is seen.
            if candidate is not None and os.path.basename(candidate[0]).startswith("python"):
                argv = candidate
                break
            time.sleep(0.05)
        assert argv is not None
        assert os.path.basename(argv[0]).startswith("python")
        assert "-c" in argv
        assert "time.sleep(30)" in " ".join(argv)
    finally:
        process.kill()
        process.wait(timeout=5)
