from __future__ import annotations

import argparse
import contextlib
import importlib
import json
import os
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from repoforge.domain.config_generation import CapabilityDeltaKind, ConfigGeneration
from repoforge.domain.errors import ConfigError
from repoforge.domain.runtime import ControlResponse, RuntimePhase, RuntimeRecord

cli = importlib.import_module("repoforge.interfaces.cli.main")


def _generation(number: int = 2) -> ConfigGeneration:
    return ConfigGeneration(
        number,
        "a" * 64,
        "b" * 64,
        (("demo", "c" * 64),),
        "now",
        "test",
        None,
        None,
        CapabilityDeltaKind.EQUIVALENT,
        number - 1 if number > 1 else None,
        "corr",
        False,
    )


def _record(phase: RuntimePhase = RuntimePhase.STARTING, *, pid: int = 10) -> RuntimeRecord:
    return RuntimeRecord(
        1,
        phase,
        pid,
        "d" * 64,
        2 if phase is RuntimePhase.HEALTHY else None,
        2,
        "repoforge",
        "e" * 64,
        "f" * 64,
        "now",
        "now",
        "corr",
        child_pid=11 if phase is RuntimePhase.HEALTHY else None,
        child_process_identity="1" * 64 if phase is RuntimePhase.HEALTHY else None,
    )


class Store:
    def __init__(self, root: Path) -> None:
        self.root = root
        self._current = _generation()
        self._active: ConfigGeneration | None = None
        self.staged: list[tuple[int, int | None]] = []

    def current(self) -> ConfigGeneration | None:
        return self._current

    def active(self) -> ConfigGeneration | None:
        return self._active

    def activation_target(self) -> None:
        return None

    def stage_activation(self, generation: int, *, expected_active: int | None) -> None:
        self.staged.append((generation, expected_active))


class RuntimeStore:
    def __init__(self, values: list[RuntimeRecord | None]) -> None:
        self.values = list(values)

    def read(self) -> RuntimeRecord | None:
        if len(self.values) > 1:
            return self.values.pop(0)
        return self.values[0] if self.values else None


class Locks:
    @contextlib.contextmanager
    def lock(self, *args: object, **kwargs: object) -> Iterator[None]:
        yield


def _args(config: Path, command: str, **kwargs: object) -> argparse.Namespace:
    values: dict[str, object] = {
        "config": str(config),
        "runtime_command": command,
        "tail": 10,
        "tunnel_id": None,
        "profile": None,
        "foreground": False,
    }
    values.update(kwargs)
    return argparse.Namespace(**values)


def test_runtime_status_logs_and_graceful_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    store = Store(tmp_path)
    monkeypatch.setattr(cli, "_ensure_generation", lambda path: store)
    monkeypatch.setattr(cli, "_runtime_status", lambda value: {"state": "healthy"})
    assert cli._runtime_command(_args(tmp_path / "config", "status")) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "healthy"

    monkeypatch.setattr(cli, "read_runtime_log", lambda path, tail: ["safe"])
    assert cli._runtime_command(_args(tmp_path / "config", "logs")) == 0
    assert json.loads(capsys.readouterr().out)["lines"] == ["safe"]

    runtime_store = RuntimeStore([_record(), None])
    monkeypatch.setattr(cli, "build_runtime_store", lambda path: runtime_store)
    monkeypatch.setattr(
        cli,
        "build_runtime_control_client",
        lambda path: SimpleNamespace(
            request=lambda request: ControlResponse(1, True, request.correlation_id, "stopping")
        ),
    )
    monkeypatch.setattr(cli, "id_generator", lambda: SimpleNamespace(new_hex=lambda n: "x" * n))
    assert cli._runtime_command(_args(tmp_path / "config", "stop")) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "stopped" and payload["forced"] is False


def test_runtime_forced_stop_and_not_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    store = Store(tmp_path)
    monkeypatch.setattr(cli, "_ensure_generation", lambda path: store)
    runtime_store = RuntimeStore([_record(), None])
    monkeypatch.setattr(cli, "build_runtime_store", lambda path: runtime_store)
    monkeypatch.setattr(
        cli,
        "build_runtime_control_client",
        lambda path: SimpleNamespace(
            request=lambda request: (_ for _ in ()).throw(ConfigError("socket down"))
        ),
    )
    monkeypatch.setattr(
        cli, "build_runtime_launcher", lambda: SimpleNamespace(force_stop=lambda *a, **k: True)
    )
    assert cli._runtime_command(_args(tmp_path / "config", "stop")) == 0
    assert json.loads(capsys.readouterr().out)["forced"] is True

    monkeypatch.setattr(cli, "build_runtime_store", lambda path: RuntimeStore([None]))
    assert cli._runtime_command(_args(tmp_path / "config", "stop")) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "not_running"


def test_runtime_start_foreground_background_and_duplicate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    store = Store(tmp_path)
    monkeypatch.setattr(cli, "_ensure_generation", lambda path: store)
    monkeypatch.setattr(cli, "_locks", lambda: Locks())
    launcher_calls: list[tuple[bool, dict[str, str]]] = []

    class Launcher:
        def start(self, path: Path, *, foreground: bool, extra_env: dict[str, str]) -> int:
            launcher_calls.append((foreground, extra_env))
            return 0 if foreground else 123

    monkeypatch.setattr(cli, "build_runtime_launcher", lambda: Launcher())
    monkeypatch.setattr(cli, "build_runtime_store", lambda path: RuntimeStore([None]))
    foreground = _args(tmp_path / "config", "start", foreground=True, tunnel_id="t", profile="p")
    assert cli._runtime_command(foreground) == 0
    assert launcher_calls[-1] == (
        True,
        {"REPOFORGE_TUNNEL_ID": "t", "REPOFORGE_TUNNEL_PROFILE": "p"},
    )

    observed = _record(RuntimePhase.HEALTHY, pid=123)
    runtime_store = RuntimeStore([None, observed])
    monkeypatch.setattr(cli, "build_runtime_store", lambda path: runtime_store)
    assert cli._runtime_command(_args(tmp_path / "config", "start")) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "healthy" and payload["pid"] == 123
    assert store.staged

    monkeypatch.setattr(
        cli, "build_runtime_store", lambda path: RuntimeStore([_record(RuntimePhase.HEALTHY)])
    )
    with pytest.raises(ConfigError, match="ALREADY_RUNNING"):
        cli._runtime_command(_args(tmp_path / "config", "start"))


def test_runtime_start_ignores_a_stale_record_from_a_previous_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A record already on disk from a prior, unrelated start attempt (e.g. one
    that failed before CONTROL_PLANE_API_KEY was set) must never be reported
    as the outcome of a fresh start; only a record whose pid matches the
    worker just spawned reflects this attempt.
    """
    store = Store(tmp_path)
    monkeypatch.setattr(cli, "_ensure_generation", lambda path: store)
    monkeypatch.setattr(cli, "_locks", lambda: Locks())
    fresh_pid = os.getpid()  # guaranteed alive for the duration of this test

    class Launcher:
        def start(self, path: Path, *, foreground: bool, extra_env: dict[str, str]) -> int:
            return fresh_pid

    monkeypatch.setattr(cli, "build_runtime_launcher", lambda: Launcher())

    stale = _record(RuntimePhase.FAILED, pid=999_999)  # never alive, previous attempt
    fresh = _record(RuntimePhase.HEALTHY, pid=fresh_pid)
    # The store keeps returning the stale record (as a real file on disk
    # would) until the new supervisor overwrites it with a fresh one.
    runtime_store = RuntimeStore([stale, stale, stale, fresh])
    monkeypatch.setattr(cli, "build_runtime_store", lambda path: runtime_store)

    assert cli._runtime_command(_args(tmp_path / "config", "start")) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "healthy"
    assert payload["pid"] == fresh_pid


def test_runtime_reload_restart_and_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    store = Store(tmp_path)
    store._active = _generation(1)
    monkeypatch.setattr(cli, "_ensure_generation", lambda path: store)
    monkeypatch.setattr(cli, "build_runtime_store", lambda path: object())
    monkeypatch.setattr(cli, "build_runtime_control_client", lambda path: object())
    monkeypatch.setattr(cli, "build_runtime_launcher", lambda: object())
    monkeypatch.setattr(cli, "id_generator", lambda: object())
    monkeypatch.setattr(cli, "system_clock", lambda: object())
    targets: list[int] = []

    @dataclass
    class Result:
        status: str
        generation: int

    class Activator:
        def __init__(self, **kwargs: object) -> None:
            pass

        def activate(self, target: ConfigGeneration, *, extra_env: dict[str, str]) -> Any:
            targets.append(target.generation)
            return Result(status="active", generation=target.generation)

    monkeypatch.setattr(cli, "GenerationActivator", Activator)
    assert cli._runtime_command(_args(tmp_path / "config", "reload")) == 0
    assert json.loads(capsys.readouterr().out)["generation"] == 2
    assert cli._runtime_command(_args(tmp_path / "config", "restart")) == 0
    assert json.loads(capsys.readouterr().out)["generation"] == 1
    assert targets == [2, 1]

    with pytest.raises(ConfigError, match="Unknown runtime"):
        cli._runtime_command(_args(tmp_path / "config", "wat"))
