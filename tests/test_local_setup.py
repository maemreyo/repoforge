from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

from repoforge.application.configuration.source import (
    SourceConfiguration,
    SourceRepository,
    parse_source,
    render_source,
)
from repoforge.domain.errors import ConfigError

cli = importlib.import_module("repoforge.interfaces.cli.main")


def test_local_only_source_round_trips_without_tunnel_section() -> None:
    source = SourceConfiguration(
        None,
        "repoforge",
        (SourceRepository("demo", "/repos/demo"),),
    )
    text = render_source(source)
    assert "[tunnel]" not in text
    assert parse_source(text) == source


def test_setup_parser_requires_explicit_local_mode_when_tunnel_is_absent() -> None:
    parser = cli.build_parser()
    local = parser.parse_args(["setup", "--local", "/repo"])
    assert local.local is True
    assert local.repos == ["/repo"]
    assert local.tunnel_id is None


def test_managed_runtime_requirement_for_local_config_is_actionable(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        render_source(SourceConfiguration(None, "repoforge", (SourceRepository("demo", "/repo"),))),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match=r"tunnel ID.*tunnel-client.*CONTROL_PLANE_API_KEY"):
        cli._require_managed_runtime_configuration(config)


def test_local_setup_rejects_conflicting_tunnel_flag(capsys: pytest.CaptureFixture[str]) -> None:
    code = cli.main(["setup", "--local", "/repo", "--tunnel-id", "tunnel"])
    assert code == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["error_code"]
    assert "--local" in payload["what_happened"]
