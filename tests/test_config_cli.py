"""`rf config get/set/edit` and `rf show-config --origin`."""

from __future__ import annotations

import argparse
import importlib
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from repoforge.application.configuration.source import SourceConfiguration, SourceRepository
from repoforge.domain.errors import ConfigError

cli = importlib.import_module("repoforge.interfaces.cli.main")


# ---------------------------------------------------------------------------
# Pure helpers: dotted-key parsing and origin classification
# ---------------------------------------------------------------------------


def test_parse_config_key_accepts_known_scalar_fields() -> None:
    assert cli._parse_config_key("repositories.demo.max_diff_lines") == ("demo", "max_diff_lines")


@pytest.mark.parametrize(
    "key",
    [
        "repositories.demo",
        "repositories.demo.max_diff_lines.extra",
        "server.max_diff_lines",
        "repositories.demo.allowed_paths",
        "repositories.demo.unknown_field",
    ],
)
def test_parse_config_key_rejects_unsupported_shapes(key: str) -> None:
    with pytest.raises(ConfigError):
        cli._parse_config_key(key)


def _source(repo: SourceRepository) -> SourceConfiguration:
    return SourceConfiguration("tunnel-1", "repoforge", (repo,))


def test_config_key_origin_reports_file_when_explicitly_overridden() -> None:
    source = _source(
        SourceRepository("demo", "/repos/demo", policy_overrides=(("max_diff_lines", "999"),))
    )
    assert cli._config_key_origin(source, "demo", "max_diff_lines") == "file"


def test_config_key_origin_reports_preset_when_templated_without_override() -> None:
    source = _source(SourceRepository("demo", "/repos/demo", policy_template="strict"))
    assert cli._config_key_origin(source, "demo", "max_diff_lines") == "preset:strict"


def test_config_key_origin_reports_default_otherwise() -> None:
    source = _source(SourceRepository("demo", "/repos/demo"))
    assert cli._config_key_origin(source, "demo", "read_only") == "default"


def test_config_key_origin_rejects_unknown_repo_id() -> None:
    source = _source(SourceRepository("demo", "/repos/demo"))
    with pytest.raises(ConfigError):
        cli._config_key_origin(source, "other", "read_only")


def test_coerce_config_value_validates_int_and_bool_types() -> None:
    assert cli._coerce_config_value("max_diff_lines", "5000") == 5000
    assert cli._coerce_config_value("read_only", "true") is True
    assert cli._coerce_config_value("read_only", "false") is False
    with pytest.raises(ConfigError):
        cli._coerce_config_value("max_diff_lines", "not-a-number")
    with pytest.raises(ConfigError):
        cli._coerce_config_value("read_only", "yes")


# ---------------------------------------------------------------------------
# `rf config get` and `rf show-config --origin` against a resolved generation
# ---------------------------------------------------------------------------


class _FakeGeneration:
    def __init__(self, generation: int, source_sha256: str) -> None:
        self.generation = generation
        self.source_sha256 = source_sha256


class _FakeStore:
    def __init__(
        self, root: Path, *, source_text: str, resolved_path: Path, source_sha256: str
    ) -> None:
        self.root = root
        self._source_text = source_text
        self._resolved_path = resolved_path
        self._current = _FakeGeneration(1, source_sha256)

    def current(self) -> _FakeGeneration:
        return self._current

    def read_source_text(self) -> str:
        return self._source_text

    def resolved_path(self, generation: int) -> Path:
        assert generation == 1
        return self._resolved_path


_SOURCE_TOML = """
[tunnel]
id = "tunnel-1"
profile = "repoforge"

[[repo]]
id = "demo"
path = "/repos/demo"
policy_overrides = ["max_diff_lines=999"]

[[repo]]
id = "other"
path = "/repos/other"
policy_template = "strict"
"""


def _resolved_toml(tmp_path: Path, repo_path: Path) -> Path:
    resolved = tmp_path / "resolved.toml"
    resolved.write_text(
        f"""
[repositories.demo]
path = "{repo_path}"
max_diff_lines = 999

[repositories.other]
path = "{repo_path}"
read_only = true
"""
    )
    return resolved


def _install_fake_store(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, repo_path: Path
) -> _FakeStore:
    store = _FakeStore(
        tmp_path,
        source_text=_SOURCE_TOML,
        resolved_path=_resolved_toml(tmp_path, repo_path),
        source_sha256=cli.sha256_text(_SOURCE_TOML),
    )
    monkeypatch.setattr(cli, "_ensure_generation", lambda config_path: store)
    return store


def test_config_get_reports_explicit_override_value_and_origin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    _install_fake_store(monkeypatch, tmp_path, repo_path)
    config_path = tmp_path / "config.toml"
    config_path.write_text("placeholder")

    assert (
        cli.main(
            ["--config", str(config_path), "config", "get", "repositories.demo.max_diff_lines"]
        )
        == 0
    )
    payload = cli.json.loads(capsys.readouterr().out)
    assert payload == {"key": "repositories.demo.max_diff_lines", "value": 999, "origin": "file"}


def test_config_get_reports_preset_origin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    _install_fake_store(monkeypatch, tmp_path, repo_path)
    config_path = tmp_path / "config.toml"
    config_path.write_text("placeholder")

    assert (
        cli.main(["--config", str(config_path), "config", "get", "repositories.other.read_only"])
        == 0
    )
    payload = cli.json.loads(capsys.readouterr().out)
    assert payload == {
        "key": "repositories.other.read_only",
        "value": True,
        "origin": "preset:strict",
    }


def test_config_get_rejects_unknown_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    _install_fake_store(monkeypatch, tmp_path, repo_path)
    config_path = tmp_path / "config.toml"
    config_path.write_text("placeholder")

    assert (
        cli.main(
            ["--config", str(config_path), "config", "get", "repositories.missing.max_diff_lines"]
        )
        == 2
    )
    payload = cli.json.loads(capsys.readouterr().out)
    assert payload["status"] == "failed"


def test_config_origins_covers_every_scalar_key_for_every_repository() -> None:
    store = _FakeStore(
        Path("/unused"),
        source_text=_SOURCE_TOML,
        resolved_path=Path("/unused/resolved.toml"),
        source_sha256="0" * 64,
    )
    origins = cli._config_origins(store)
    assert origins["demo"]["max_diff_lines"] == "file"
    assert origins["other"]["read_only"] == "preset:strict"
    for repo_id in ("demo", "other"):
        for field in cli._CONFIG_SCALAR_KEYS:
            assert origins[repo_id][field] in {"file"} | {
                f"preset:{t}" for t in ("strict", "standard", "relaxed")
            } | {"default"}


# ---------------------------------------------------------------------------
# `rf config set` delegates to the governed repo-refresh pipeline
# ---------------------------------------------------------------------------


def test_config_set_builds_a_single_repo_policy_override_refresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, argparse.Namespace] = {}

    def fake_refresh(args: argparse.Namespace) -> int:
        captured["args"] = args
        return 0

    monkeypatch.setattr(cli, "_repo_refresh", fake_refresh)
    config_path = tmp_path / "config.toml"
    args = SimpleNamespace(
        key="repositories.demo.max_diff_lines", value="5000", approve=[], activate="auto"
    )

    assert cli._config_set(config_path, args) == 0
    built = captured["args"]
    assert built.config == str(config_path)
    assert built.repo_id == "demo"
    assert built.policy_override == ["demo.max_diff_lines=5000"]
    assert built.accept is True
    assert built.template is None
    assert built.activate == "auto"


def test_config_set_lowercases_boolean_override_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, argparse.Namespace] = {}

    def fake_refresh(args: argparse.Namespace) -> int:
        captured["args"] = args
        return 0

    monkeypatch.setattr(cli, "_repo_refresh", fake_refresh)
    config_path = tmp_path / "config.toml"
    args = SimpleNamespace(
        key="repositories.demo.read_only", value="true", approve=[], activate="auto"
    )

    cli._config_set(config_path, args)
    assert captured["args"].policy_override == ["demo.read_only=true"]


def test_config_set_rejects_invalid_value_before_touching_refresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_refresh(args: argparse.Namespace) -> int:
        raise AssertionError("refresh must not run when the value is invalid")

    monkeypatch.setattr(cli, "_repo_refresh", fail_refresh)
    config_path = tmp_path / "config.toml"
    args = SimpleNamespace(
        key="repositories.demo.max_diff_lines", value="not-a-number", approve=[], activate="auto"
    )

    with pytest.raises(ConfigError):
        cli._config_set(config_path, args)


# ---------------------------------------------------------------------------
# `rf config edit`
# ---------------------------------------------------------------------------


def _editor_script(tmp_path: Path, body: str) -> str:
    script = tmp_path / "fake_editor.py"
    script.write_text(body)
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return f"{sys.executable} {script}"


def test_config_edit_rejects_when_editor_exits_non_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.toml"
    original = '[[repo]]\nid = "demo"\npath = "/repos/demo"\n'
    config_path.write_text(original)
    monkeypatch.setenv("EDITOR", "false")

    with pytest.raises(ConfigError, match="Editor exited"):
        cli._config_edit(config_path)
    assert config_path.read_text() == original


def test_config_edit_preserves_original_and_writes_sidecar_on_invalid_toml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.toml"
    original = '[[repo]]\nid = "demo"\npath = "/repos/demo"\n'
    config_path.write_text(original)
    monkeypatch.setenv(
        "EDITOR",
        _editor_script(
            tmp_path,
            "import sys, pathlib\npathlib.Path(sys.argv[1]).write_text('not [ valid toml')\n",
        ),
    )

    with pytest.raises(ConfigError, match="invalid"):
        cli._config_edit(config_path)
    assert config_path.read_text() == original
    sidecar = config_path.with_name(config_path.name + ".rej")
    assert sidecar.read_text() == "not [ valid toml"


def test_config_edit_reports_unchanged_when_editor_saves_identical_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path = tmp_path / "config.toml"
    original = '[[repo]]\nid = "demo"\npath = "/repos/demo"\n'
    config_path.write_text(original)
    monkeypatch.setenv("EDITOR", "true")

    assert cli._config_edit(config_path) == 0
    payload = cli.json.loads(capsys.readouterr().out)
    assert payload == {"status": "unchanged", "config": str(config_path)}
    assert config_path.read_text() == original


def test_config_edit_saves_valid_changes_and_reports_staleness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path = tmp_path / "config.toml"
    original = '[[repo]]\nid = "demo"\npath = "/repos/demo"\n'
    config_path.write_text(original)
    edited = (
        '[[repo]]\nid = "demo"\npath = "/repos/demo"\npolicy_overrides = ["max_diff_lines=5000"]\n'
    )
    monkeypatch.setenv(
        "EDITOR",
        _editor_script(
            tmp_path,
            f"import sys, pathlib\npathlib.Path(sys.argv[1]).write_text({edited!r})\n",
        ),
    )
    monkeypatch.setattr(
        cli,
        "_store",
        lambda config_path: SimpleNamespace(current=lambda: _FakeGeneration(1, "0" * 64)),
    )

    assert cli._config_edit(config_path) == 0
    payload = cli.json.loads(capsys.readouterr().out)
    assert payload["status"] == "saved"
    assert payload["stale"] is True
    assert "rf repo refresh --accept" in payload["safe_next_action"]
    assert config_path.read_text() == edited
