from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from repoforge.errors import ConfigError
from repoforge.interfaces.mcp.server import create_server, tool_surface_hash
from repoforge.runtime import (
    clear_runtime_state,
    managed_start_claim,
    read_managed_runtime,
    read_runtime_log,
    read_runtime_state,
    stop_managed_runtime,
    write_managed_runtime,
    write_runtime_state,
)


class FakeService:
    def __getattr__(self, name: str) -> Any:
        if name.startswith(("repo_", "workspace_")):
            return lambda *args, **kwargs: {"name": name, "args": list(args), "kwargs": kwargs}
        raise AttributeError(name)


def test_mcp_server_registration_executes_complete_tool_surface() -> None:
    server = create_server(service=FakeService())  # type: ignore[arg-type]
    assert server.name == "RepoForge"
    assert len(tool_surface_hash()) == 64


def test_runtime_state_lifecycle_and_validation(tmp_path: Path) -> None:
    path = tmp_path / "runtime.json"
    assert read_runtime_state(path) is None
    with pytest.raises(ConfigError, match="positive"):
        write_runtime_state(path, 0)
    state = write_runtime_state(path, 3, "surface")
    assert read_runtime_state(path) == state
    clear_runtime_state(path, state.pid + 1)
    assert path.exists()
    clear_runtime_state(path, state.pid)
    assert not path.exists()

    path.write_text("[]", encoding="utf-8")
    with pytest.raises(ConfigError, match="JSON object"):
        read_runtime_state(path)
    path.write_text("{bad", encoding="utf-8")
    with pytest.raises(ConfigError, match="Cannot read runtime state"):
        read_runtime_state(path)
    path.write_text(json.dumps({"pid": os.getpid(), "active_generation": 1, "started_at": "now"}))
    assert read_runtime_state(path) is None and not path.exists()
    path.write_text(
        json.dumps(
            {"pid": -1, "active_generation": 1, "started_at": "now", "process_identity": "x" * 64}
        )
    )
    with pytest.raises(ConfigError, match="invalid"):
        read_runtime_state(path)


def test_managed_runtime_lifecycle_and_start_lock(tmp_path: Path) -> None:
    state_path = tmp_path / "managed.json"
    assert read_managed_runtime(state_path) is None
    with pytest.raises(ConfigError, match="valid process"):
        write_managed_runtime(state_path, pid=0, generation=1, profile="p", executable="python")
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True
    )
    try:
        managed = write_managed_runtime(
            state_path,
            pid=process.pid,
            generation=2,
            profile="repoforge",
            executable=sys.executable,
        )
        assert read_managed_runtime(state_path) == managed
        stopped = stop_managed_runtime(state_path, timeout_seconds=2)
        process.wait(timeout=5)
        assert stopped is not None and stopped.pid == process.pid
        assert read_managed_runtime(state_path) is None
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()

    state_path.write_text("{}", encoding="utf-8")
    assert read_managed_runtime(state_path) is None and not state_path.exists()
    claim = tmp_path / "start.lock"
    with (
        managed_start_claim(claim),
        pytest.raises(ConfigError, match="ALREADY_STARTING"),
        managed_start_claim(claim),
    ):
        pass
    with managed_start_claim(claim):
        pass


def test_runtime_log_bounds_redaction_and_io_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "runtime.log"
    assert read_runtime_log(path, 1) == []
    for invalid in (0, 1001):
        with pytest.raises(ConfigError, match="between"):
            read_runtime_log(path, invalid)
    path.write_text("first\ntoken=abc\npassword: xyz\nlast\n", encoding="utf-8")
    assert read_runtime_log(path, 3) == ["token=<redacted>", "password: <redacted>", "last"]
    original_open = Path.open

    def broken_open(self: Path, *args: object, **kwargs: object):
        if self == path:
            raise OSError("injected")
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", broken_open)
    with pytest.raises(ConfigError, match="Cannot read runtime log"):
        read_runtime_log(path, 1)
