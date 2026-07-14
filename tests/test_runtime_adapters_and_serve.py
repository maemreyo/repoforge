from __future__ import annotations

import hashlib
import importlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from repoforge.adapters.filesystem.local import LocalFileSystem
from repoforge.adapters.runtime.launcher import SubprocessRuntimeLauncher
from repoforge.adapters.runtime.profile_store import JsonTunnelProfileStore
from repoforge.adapters.runtime.state_store import JsonRuntimeStore
from repoforge.adapters.runtime.tunnel_cli import TunnelCliClient
from repoforge.domain.errors import ConfigError
from repoforge.domain.runtime import (
    ControlCommand,
    ControlRequest,
    RuntimePhase,
    RuntimeRecord,
    TunnelProfile,
)
from repoforge.testing import InMemoryOperationGate

cli = importlib.import_module("repoforge.interfaces.cli.main")


def _profile(executable: str = "tunnel-client") -> TunnelProfile:
    return TunnelProfile("a" * 64, "repoforge", executable, "1.2.3", ("rf", "serve"))


def _record(*, pid: int | None = None, child_pid: int | None = None) -> RuntimeRecord:
    return RuntimeRecord(
        1,
        RuntimePhase.STARTING,
        pid,
        "b" * 64 if pid is not None else None,
        None,
        2,
        "repoforge",
        "c" * 64,
        "d" * 64,
        "now" if pid is not None else None,
        "now",
        "corr",
        child_pid=child_pid,
        child_process_identity="e" * 64 if child_pid is not None else None,
    )


def test_tunnel_profile_store_round_trip_and_rejects_corruption(tmp_path: Path) -> None:
    path = tmp_path / "profile.json"
    store = JsonTunnelProfileStore(path, LocalFileSystem())
    assert store.fingerprint() is None

    profile = _profile()
    store.commit(profile)
    assert store.fingerprint() == profile.fingerprint
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert "tunnel_id" not in payload
    assert payload["mcp_argv_sha256"] == hashlib.sha256(b"rf\0serve").hexdigest()

    path.write_text("[]", encoding="utf-8")
    with pytest.raises(ConfigError, match="must be an object"):
        store.fingerprint()
    path.write_text('{"fingerprint":"short"}', encoding="utf-8")
    with pytest.raises(ConfigError, match="invalid"):
        store.fingerprint()
    path.write_text("{", encoding="utf-8")
    with pytest.raises(ConfigError, match="Invalid tunnel profile"):
        store.fingerprint()


def test_runtime_store_round_trip_degrades_child_and_clears_stale_supervisor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = importlib.import_module("repoforge.adapters.runtime.state_store")
    path = tmp_path / "runtime.json"
    store = JsonRuntimeStore(path)
    assert store.read() is None

    identities = {10: "b" * 64, 11: "e" * 64}
    monkeypatch.setattr(module, "process_identity", lambda pid: identities.get(pid))
    record = _record(pid=10, child_pid=11)
    store.write(record)
    assert store.read() == record

    identities.pop(11)
    degraded = store.read()
    assert degraded is not None
    assert degraded.phase is RuntimePhase.DEGRADED
    assert degraded.child_pid is None
    assert degraded.last_error_code == "CHILD_IDENTITY_MISMATCH"

    identities.pop(10)
    assert store.read() is None
    assert not path.exists()

    path.write_text("[]", encoding="utf-8")
    with pytest.raises(ConfigError, match="must be an object"):
        store.read()
    path.write_text("{}", encoding="utf-8")
    with pytest.raises(ConfigError, match="Invalid runtime state fields"):
        store.read()
    path.write_text("{", encoding="utf-8")
    with pytest.raises(ConfigError, match="Invalid runtime state"):
        store.read()


def test_runtime_store_clear_preserves_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = importlib.import_module("repoforge.adapters.runtime.state_store")
    monkeypatch.setattr(module, "process_identity", lambda pid: "b" * 64 if pid == 10 else None)
    store = JsonRuntimeStore(tmp_path / "runtime.json")
    store.write(_record(pid=10))
    store.clear(expected_pid=20)
    assert store.read() is not None
    store.clear(expected_pid=10)
    assert store.read() is None


def _write_fake_tunnel(path: Path) -> None:
    path.write_text(
        """#!/usr/bin/env python3
import os
import sys
import time
args = sys.argv[1:]
if args == ['--version']:
    print('tunnel-client 9.9')
    raise SystemExit(0)
if args and args[0] == 'init':
    print('initialized')
    raise SystemExit(0)
if args and args[0] == 'doctor':
    if os.environ.get('FAIL_DOCTOR'):
        print('token=' + os.environ.get('CONTROL_PLANE_API_KEY', ''), file=sys.stderr)
        raise SystemExit(7)
    print('healthy')
    raise SystemExit(0)
if args and args[0] == 'run':
    print('running', flush=True)
    time.sleep(60)
raise SystemExit(2)
""",
        encoding="utf-8",
    )
    path.chmod(0o755)


def test_tunnel_cli_full_lifecycle_and_redaction(tmp_path: Path) -> None:
    executable = tmp_path / "tunnel-client"
    _write_fake_tunnel(executable)
    client = TunnelCliClient(str(executable), default_timeout_seconds=5)
    profile = _profile(str(executable))
    env = {
        "PATH": os.environ.get("PATH", ""),
        "REPOFORGE_TUNNEL_ID": "tunnel-secret-id",
        "CONTROL_PLANE_API_KEY": "super-secret",
    }

    assert "9.9" in client.executable_version()
    client.initialize(profile, env=env)
    ok, detail = client.doctor(profile, env=env)
    assert ok and "healthy" in detail
    failed, detail = client.doctor(profile, env={**env, "FAIL_DOCTOR": "1"})
    assert failed is False
    assert "super-secret" not in detail and "<redacted>" in detail

    log = tmp_path / "runtime.log"
    log.write_bytes(b"x" * 5_000_001)
    child = client.start(profile, env=env, log_path=log)
    assert log.with_suffix(".log.1").is_file()
    assert client.is_alive(child)
    client.terminate(child, grace_seconds=0.1)
    for _ in range(100):
        if not client.is_alive(child):
            break
        import time

        time.sleep(0.01)
    assert not client.is_alive(child)

    with pytest.raises(ConfigError, match="Tunnel id"):
        client.initialize(profile, env={})


def test_tunnel_cli_reports_version_and_execution_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = importlib.import_module("repoforge.adapters.runtime.tunnel_cli")
    client = TunnelCliClient("missing")

    def boom(*args: object, **kwargs: object) -> Any:
        raise OSError("no executable")

    monkeypatch.setattr(module.subprocess, "run", boom)
    with pytest.raises(ConfigError, match="Cannot inspect"):
        client.executable_version()
    with pytest.raises(ConfigError, match="failed to execute"):
        client._run(["missing"], env={}, timeout=1)


def test_runtime_launcher_foreground_background_and_identity_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = importlib.import_module("repoforge.adapters.runtime.launcher")
    launcher = SubprocessRuntimeLauncher()
    calls: list[tuple[list[str], dict[str, str]]] = []

    monkeypatch.setenv("CONTROL_PLANE_API_KEY", "key")
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda argv, env, check: calls.append((argv, env)) or SimpleNamespace(returncode=4),
    )
    assert launcher.start(tmp_path / "config.toml", foreground=True, extra_env={"X": "1"}) == 4
    assert calls[0][1]["X"] == "1" and calls[0][1]["CONTROL_PLANE_API_KEY"] == "key"

    class FakePopen:
        pid = 321

    monkeypatch.setattr(module.subprocess, "Popen", lambda *args, **kwargs: FakePopen())
    assert launcher.start(tmp_path / "config.toml", foreground=False, extra_env={}) == 321

    record = _record(pid=10)
    monkeypatch.setattr(module, "process_identity", lambda pid: None)
    assert launcher.force_stop(record, grace_seconds=0) is False

    values = iter(["b" * 64, None])
    monkeypatch.setattr(module, "process_identity", lambda pid: next(values, None))
    monkeypatch.setattr(module.os, "killpg", lambda pid, sig: None)
    assert launcher.force_stop(record, grace_seconds=0.01) is True


def test_serve_control_handler_covers_health_drain_resume_and_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mcp_module = importlib.import_module("repoforge.interfaces.mcp.server")
    gate = InMemoryOperationGate()
    active = SimpleNamespace(generation=7)

    class Store:
        root = tmp_path

        def activation_target(self) -> object:
            return active

        def active(self) -> object:
            return active

        def resolved_path(self, generation: int) -> Path:
            assert generation == 7
            return tmp_path / "resolved.toml"

    store = Store()
    captured: dict[str, Any] = {}

    class Control:
        def start(self, handler: Any) -> None:
            captured["handler"] = handler

        def close(self) -> None:
            captured["closed"] = True

    class Service:
        def repo_list(self) -> dict[str, Any]:
            return {"repositories": [{"repo_id": "demo"}]}

    class MCP:
        def run(self, *, transport: str) -> None:
            assert transport == "stdio"
            handler = captured["handler"]
            assert handler(ControlRequest(1, ControlCommand.PING, "p")).ok
            assert handler(ControlRequest(1, ControlCommand.STATUS, "s")).ok
            assert handler(ControlRequest(1, ControlCommand.HEALTH, "h")).status == "healthy"
            invalid = handler(
                ControlRequest(1, ControlCommand.DRAIN, "d", (("timeout_seconds", 999),))
            )
            assert invalid.error_code == "INVALID_DRAIN_TIMEOUT"
            drained = handler(
                ControlRequest(1, ControlCommand.DRAIN, "d2", (("timeout_seconds", 0),))
            )
            assert drained.status == "drained"
            assert handler(ControlRequest(1, ControlCommand.RESUME, "r")).status == "open"
            assert (
                handler(
                    ControlRequest(1, ControlCommand.FAIL_CLOSED, "f", (("reason", "test"),))
                ).status
                == "fail_closed"
            )
            assert (
                handler(ControlRequest(1, ControlCommand.SHUTDOWN, "u")).error_code
                == "UNSUPPORTED_CONTROL_COMMAND"
            )

    monkeypatch.setattr(cli, "_ensure_generation", lambda path: store)
    monkeypatch.setattr(cli, "load_config", lambda path: object())
    monkeypatch.setattr(cli, "build_operation_gate", lambda: gate)
    monkeypatch.setattr(cli, "build_application", lambda config, overrides: object())
    monkeypatch.setattr(cli, "CodingService", lambda config, application: Service())
    monkeypatch.setattr(cli, "build_runtime_control_server", lambda path: Control())
    monkeypatch.setattr(
        cli,
        "write_runtime_state",
        lambda path, generation, surface: SimpleNamespace(pid=55),
    )
    monkeypatch.setattr(cli, "clear_runtime_state", lambda path, pid: captured.update(cleared=pid))
    monkeypatch.setattr(mcp_module, "tool_surface_hash", lambda: "surface")
    monkeypatch.setattr(mcp_module, "create_server", lambda *, router: MCP())

    assert cli._serve(tmp_path / "config.toml") == 0
    assert captured["closed"] is True and captured["cleared"] == 55


def test_serve_health_failure_and_missing_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class EmptyStore:
        def activation_target(self) -> None:
            return None

        def active(self) -> None:
            return None

        def current(self) -> None:
            return None

    monkeypatch.setattr(cli, "_ensure_generation", lambda path: EmptyStore())
    with pytest.raises(ConfigError, match="No accepted configuration generation"):
        cli._serve(tmp_path / "config.toml")
