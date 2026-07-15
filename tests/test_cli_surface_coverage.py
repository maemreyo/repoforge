from __future__ import annotations

import argparse
import importlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from repoforge.application.configuration.source import SourceConfiguration, SourceRepository
from repoforge.domain.config_generation import CapabilityDeltaKind, ConfigGeneration
from repoforge.domain.errors import ConfigError
from repoforge.domain.runtime import RuntimePhase, RuntimeRecord

cli = importlib.import_module("repoforge.interfaces.cli.main")


def _generation(number: int = 1, *, active: bool = False) -> ConfigGeneration:
    return ConfigGeneration(
        number,
        "a" * 64,
        "b" * 64,
        (("demo", "c" * 64),),
        "2026-07-13T00:00:00+00:00",
        "test",
        None,
        None,
        CapabilityDeltaKind.EQUIVALENT,
        number - 1 or None,
        "corr",
        active,
    )


class FakeStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.source_path = root / "config.toml"
        self.active_resolved_path = root / "resolved.toml"
        self._current = _generation()
        self._active = _generation(active=True)
        self.rollback_calls: list[tuple[int, int | None, str | None]] = []

    def current(self) -> ConfigGeneration | None:
        return self._current

    def active(self) -> ConfigGeneration | None:
        return self._active

    def activation_target(self) -> ConfigGeneration | None:
        return None

    def history(self) -> tuple[ConfigGeneration, ...]:
        return (self._current,)

    def rollback(
        self, generation: int, *, expected_active: int | None, approval_token: str | None = None
    ) -> ConfigGeneration:
        self.rollback_calls.append((generation, expected_active, approval_token))
        return self._current

    def resolved_path(self, generation: int) -> Path:
        del generation
        return self.active_resolved_path


class FakeService:
    config = SimpleNamespace(source_path=Path("/config"))
    audit: Any = None
    metrics: Any = None

    def repo_list(self) -> dict[str, Any]:
        return {"repositories": [{"repo_id": "demo"}]}

    def doctor(self) -> dict[str, Any]:
        return {"ok": True, "checks": [], "summary": {"total": 0}}

    def workspace_list(self) -> dict[str, Any]:
        return {"workspaces": []}


def _fake_service_with_audit(tmp_path: Path) -> FakeService:
    service = FakeService()
    audit_path = tmp_path / "state" / "audit.jsonl"
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit_path.write_text(
        json.dumps(
            {"action": "workspace_create", "success": True, "details": {"duration_ms": 12.5}}
        )
        + "\n",
        encoding="utf-8",
    )
    service.audit = SimpleNamespace(path=audit_path)
    service.metrics = SimpleNamespace(
        path=tmp_path / "state" / "operation-metrics.json",
        snapshot=lambda: {"version": 1, "operations": {}},
    )
    return service


def test_cli_helpers_and_rendering(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli._human_lines({"a": 1, "b": [2, {"c": 3}]})
    cli._OUTPUT_FORMAT = "json"
    cli._json({"ok": True})
    assert json.loads(capsys.readouterr().out) == {"ok": True}
    cli._OUTPUT_FORMAT = "human"
    cli._json({"ok": True, "items": ["a"]})
    assert "ok: True" in capsys.readouterr().out
    cli._OUTPUT_FORMAT = "json"

    assert cli._parse_decisions(["a=b", "demo.x=y"]) == {"a": "b", "demo.x": "y"}
    with pytest.raises(ValueError, match="CODE=CHOICE"):
        cli._parse_decisions(["bad"])
    decisions = {"global": "g", "demo.scoped": "s", "other.x": "o"}
    assert cli._decisions_for_repo(decisions, "demo") == {"global": "g", "scoped": "s"}
    assert cli._parse_overrides(["x=y"]) == {"x": "y"}
    assert cli._overrides_for_repo({"demo.x": "y"}, "demo") == {"x": "y"}
    assert cli._approval_map(["", "a", "a"]) == {"a"}
    assert cli._runtime_environment(argparse.Namespace(tunnel_id="t", profile="p")) == {
        "REPOFORGE_TUNNEL_ID": "t",
        "REPOFORGE_TUNNEL_PROFILE": "p",
    }
    assert cli._runtime_environment(argparse.Namespace(tunnel_id=None, profile=None)) == {}
    assert cli._normalize_global_config(["runtime", "status", "--config", "/tmp/x"]) == [
        "--config",
        "/tmp/x",
        "runtime",
        "status",
    ]
    assert cli._normalize_global_config(["--config=/tmp/x", "doctor"]) == [
        "--config",
        "/tmp/x",
        "doctor",
    ]
    assert cli._normalize_global_config(["doctor", "--config"]) == ["doctor", "--config"]


def test_main_dispatches_all_command_families(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    store = FakeStore(tmp_path)
    store.source_path.write_text("x", encoding="utf-8")
    store.active_resolved_path.write_text("x", encoding="utf-8")
    monkeypatch.setattr(cli, "_ensure_generation", lambda path: store)
    monkeypatch.setattr(
        cli,
        "_source_for_display",
        lambda value: SourceConfiguration("t", "p", (SourceRepository("demo", "/repo"),)),
    )
    monkeypatch.setattr(cli, "_activation_result", lambda *args: {"runtime_state": "stopped"})
    monkeypatch.setattr(cli, "_activate", lambda *args, **kwargs: {"activation": "skipped"})
    monkeypatch.setattr(cli, "_runtime_status", lambda value: {"state": "stopped"})
    monkeypatch.setattr(
        cli, "write_private_file", lambda path, data, mode=0o600: path.write_bytes(data)
    )
    monkeypatch.setattr(cli, "load_config", lambda path: object())
    monkeypatch.setattr(cli, "CodingService", lambda config: _fake_service_with_audit(tmp_path))
    monkeypatch.setattr(cli, "system_clock", lambda: SimpleNamespace(now_iso=lambda: "now"))

    called: list[str] = []
    for name in (
        "_serve",
        "_setup",
        "_repo_inspect",
        "_repo_propose",
        "_repo_enroll",
        "_repo_remove",
        "_repo_refresh",
    ):
        monkeypatch.setattr(
            cli, name, lambda *args, _name=name, **kwargs: called.append(_name) or 0
        )
    monkeypatch.setattr(
        cli, "_runtime_command", lambda args: called.append(f"runtime:{args.runtime_command}") or 0
    )

    config = str(store.source_path)
    commands = [
        ["--config", config, "serve"],
        ["--config", config, "start", "--background"],
        ["--config", config, "setup", "/repo", "--tunnel-id", "t"],
        ["--config", config, "repo", "inspect", "/repo"],
        ["--config", config, "repo", "propose", "/repo"],
        ["--config", config, "repo", "enroll", "/repo"],
        ["--config", config, "repo", "add", "/repo"],
        ["--config", config, "repo", "remove", "demo"],
        ["--config", config, "repo", "refresh"],
        ["--config", config, "runtime", "status"],
    ]
    for argv in commands:
        assert cli.main(argv) == 0
    assert {
        "_serve",
        "_setup",
        "_repo_inspect",
        "_repo_propose",
        "_repo_enroll",
        "_repo_remove",
        "_repo_refresh",
    }.issubset(called)
    assert "runtime:start" in called and "runtime:status" in called

    assert cli.main(["--config", config, "repo", "list"]) == 0
    assert json.loads(capsys.readouterr().out)["repositories"][0]["repo_id"] == "demo"
    assert cli.main(["--config", config, "config", "history"]) == 0
    assert "generations" in json.loads(capsys.readouterr().out)
    assert cli.main(["--config", config, "config", "rollback", "1", "--approve", "token"]) == 0
    assert store.rollback_calls == [(1, 1, "token")]
    capsys.readouterr()

    output = tmp_path / "bundle.json"
    assert cli.main(["--config", config, "diagnostics", "bundle", "--output", str(output)]) == 0
    assert output.is_file()
    bundle = json.loads(output.read_text(encoding="utf-8"))
    assert bundle["schema_version"] == 1
    assert bundle["capabilities"]["ok"] is True
    assert bundle["metrics"]["operations"] == {}
    assert "runtime log content" in bundle["exclusions"]
    capsys.readouterr()
    assert cli.main(["--config", config, "show-config"]) == 0
    assert cli.main(["--config", config, "doctor"]) == 0
    assert cli.main(["--config", config, "list-workspaces"]) == 0

    capsys.readouterr()
    assert cli.main(["--config", config, "audit", "--last", "5"]) == 0
    audit_payload = json.loads(capsys.readouterr().out)
    assert audit_payload["events"][0]["action"] == "workspace_create"

    assert cli.main(["--config", config, "audit", "stats"]) == 0
    stats_payload = json.loads(capsys.readouterr().out)
    assert stats_payload["path"].endswith("operation-metrics.json")
    assert stats_payload["operations"] == []


def test_audit_stats_requires_configured_metrics_sink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    store = FakeStore(tmp_path)
    store.source_path.write_text("x", encoding="utf-8")
    store.active_resolved_path.write_text("x", encoding="utf-8")
    monkeypatch.setattr(cli, "_ensure_generation", lambda path: store)
    monkeypatch.setattr(cli, "load_config", lambda path: object())
    service = FakeService()
    service.audit = SimpleNamespace(path=tmp_path / "audit.jsonl")
    service.metrics = None
    monkeypatch.setattr(cli, "CodingService", lambda config: service)

    code = cli.main(["--config", str(store.source_path), "audit", "stats"])
    assert code == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "failed"


def test_main_returns_stable_error_envelope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        cli, "_repo_inspect", lambda args: (_ for _ in ()).throw(ConfigError("token=secret"))
    )
    code = cli.main(["--config", str(tmp_path / "x"), "repo", "inspect", "/repo"])
    assert code == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "failed"
    assert "secret" not in payload["what_happened"]


def test_runtime_status_and_activation_noop_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = FakeStore(tmp_path)
    monkeypatch.setattr(cli, "build_runtime_store", lambda path: SimpleNamespace(read=lambda: None))
    status = cli._runtime_status(store)
    assert status["state"] == "stopped" and status["restart_required"] is True
    assert cli._activation_result(store, 1)["runtime_state"] == "stopped"
    assert (
        cli._activate(store, store.source_path, store.current(), mode="never")["restart_required"]
        is False
    )  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="Unsupported"):
        cli._activate(store, store.source_path, store.current(), mode="bad")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="no-wait"):
        cli._activate(store, store.source_path, store.current(), mode="always", wait=False)  # type: ignore[arg-type]

    healthy = RuntimeRecord(
        1,
        RuntimePhase.HEALTHY,
        10,
        "d" * 64,
        1,
        1,
        "p",
        "e" * 64,
        cli._tool_surface_rediscovery(None)["current_tool_surface_hash"] or "",
        "now",
        "now",
        "corr",
        child_pid=11,
        child_process_identity="f" * 64,
        health=(("mcp", True, "ok"),),
    )
    monkeypatch.setattr(
        cli, "build_runtime_store", lambda path: SimpleNamespace(read=lambda: healthy)
    )
    status = cli._runtime_status(store)
    assert status["state"] == "healthy" and status["safe_next_action"] == "Runtime is healthy."
