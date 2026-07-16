from __future__ import annotations

import importlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

cli = importlib.import_module("repoforge.interfaces.cli.main")


class _Store:
    def __init__(self, root: Path) -> None:
        self.source_path = root / "config.toml"
        self.resolved = root / "resolved.toml"
        self.source_path.write_text("x", encoding="utf-8")
        self.resolved.write_text("x", encoding="utf-8")
        self.generation = SimpleNamespace(generation=1)

    def active(self) -> Any:
        return self.generation

    def current(self) -> Any:
        return self.generation

    def resolved_path(self, generation: int) -> Path:
        assert generation == 1
        return self.resolved


def test_ticket_sync_parser_defaults_to_dry_run() -> None:
    args = cli.build_parser().parse_args(
        [
            "tickets",
            "sync",
            "--repo-id",
            "repoforge",
            "--owner",
            "maemreyo",
            "--project-number",
            "7",
        ]
    )

    assert args.command == "tickets"
    assert args.tickets_command == "sync"
    assert args.apply is False
    assert args.owner_type == "organization"


def test_ticket_sync_cli_routes_explicit_apply(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = _Store(tmp_path)
    calls: list[dict[str, Any]] = []

    class Service:
        def ticket_project_sync(self, **kwargs: Any) -> dict[str, Any]:
            calls.append(kwargs)
            return {"status": "applied", "mode": "apply"}

    monkeypatch.setattr(cli, "_ensure_generation", lambda path: store)
    monkeypatch.setattr(cli, "load_config", lambda path: object())
    monkeypatch.setattr(cli, "CodingService", lambda config: Service())

    code = cli.main(
        [
            "--config",
            str(store.source_path),
            "tickets",
            "sync",
            "--repo-id",
            "repoforge",
            "--owner",
            "maemreyo",
            "--project-number",
            "7",
            "--owner-type",
            "user",
            "--apply",
            "--idempotency-key",
            "issue-63",
        ]
    )

    assert code == 0
    assert json.loads(capsys.readouterr().out)["status"] == "applied"
    assert calls == [
        {
            "repo_id": "repoforge",
            "owner": "maemreyo",
            "project_number": 7,
            "owner_type": "user",
            "apply": True,
            "idempotency_key": "issue-63",
        }
    ]
