from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

from repoforge.application.configuration.paths import resolve_repoforge_paths

cli = importlib.import_module("repoforge.interfaces.cli.main")


def test_resolve_repoforge_paths_is_absolute_deterministic_and_read_only(tmp_path: Path) -> None:
    config = tmp_path / "config" / "config.toml"
    state = tmp_path / "state"

    first = resolve_repoforge_paths(config, state_root=state)
    second = resolve_repoforge_paths(config, state_root=state)

    assert first == second
    assert first.config_file == config.resolve()
    assert first.state_root == state.resolve()
    assert first.generation_root.parent.parent == state.resolve() / "config-locks"
    assert first.onboarding_root == state.resolve() / "onboarding"
    assert first.audit_log == state.resolve() / "audit.jsonl"
    assert first.runtime_log.name == "managed-runtime.log"
    assert not config.exists()
    assert not state.exists()


def test_config_path_command_works_before_configuration_exists(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = tmp_path / "missing" / "config.toml"
    monkeypatch.setenv("REPOFORGE_CONFIG", str(config))
    assert cli.main(["--config", str(config), "config", "path"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["config_file"]["path"] == str(config.resolve())
    assert payload["config_file"]["exists"] is False
    assert payload["config_file"]["overridden_by"] == "REPOFORGE_CONFIG"
    assert payload["state_root"]["path"].endswith("repoforge")
    assert "generation_root" in payload


def test_parser_and_release_contract_expose_config_path() -> None:
    parsed = cli.build_parser().parse_args(["config", "path"])
    assert parsed.config_command == "path"
    from repoforge.interfaces.cli.contract import build_cli_release_contract

    contract = build_cli_release_contract()
    assert contract["commands"]["config"]["actions"] == ["path", "history", "rollback"]


def test_user_path_literals_have_one_source_of_truth() -> None:
    package = Path(__file__).parents[1] / "src/repoforge"
    matches: list[str] = []
    for path in package.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if ".config/repoforge" in text or ".local/state/repoforge" in text:
            matches.append(str(path.relative_to(package)))
    assert matches == ["domain/user_paths.py"]
