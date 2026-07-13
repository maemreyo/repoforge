from __future__ import annotations

import importlib
from pathlib import Path


def test_compatibility_facades_export_expected_symbols(tmp_path: Path, monkeypatch) -> None:
    audit = importlib.import_module("repoforge.audit")
    cli = importlib.import_module("repoforge.cli")
    delta = importlib.import_module("repoforge.config_delta")
    runner = importlib.import_module("repoforge.runner")
    security = importlib.import_module("repoforge.security")
    server = importlib.import_module("repoforge.server")
    state = importlib.import_module("repoforge.state")
    worker = importlib.import_module("repoforge.runtime_worker")

    assert audit.AuditLogger is audit.JsonlAuditSink
    assert callable(cli.main) and callable(cli.build_parser)
    assert delta.CapabilityDeltaKind.EQUIVALENT.value == "equivalent"
    assert runner.CommandRunner is runner.SubprocessCommandExecutor
    assert security.slugify("Hello World") == "hello-world"
    assert callable(server.create_server) and server.tool_surface_hash()
    assert state.utc_now()
    store = state.StateStore(tmp_path)
    with store.lock("demo"):
        pass

    monkeypatch.setattr(worker, "run_runtime_worker", lambda path: 7)
    assert worker.main(["--config", str(tmp_path / "config.toml")]) == 7
