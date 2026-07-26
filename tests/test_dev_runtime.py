"""Tests for #271: isolated dev runtime layout, config provisioning, and service."""

from __future__ import annotations

from pathlib import Path

import pytest
import tomli as tomllib

from repoforge.adapters.activation.dev_config import TomlDevConfigProvisioner, render_flat_toml
from repoforge.application.activation.dev_runtime import (
    DevRuntimeService,
    derive_dev_runtime_layout,
    dev_runtime_record_path,
)
from repoforge.domain.errors import ConfigError

_BASE_CONFIG = """
[server]
state_root = "/home/dev/.local/state/repoforge"
max_background_profiles = 2

[tunnel]
id = "prod-tunnel"
profile = "repoforge"

[[repo]]
id = "app"
path = "/home/dev/app"

[repositories.app.profiles.full]
commands = [["make", "test"]]
"""


# --------------------------------------------------------------------------- layout


def test_layout_isolates_state_config_and_tunnel(tmp_path: Path) -> None:
    layout = derive_dev_runtime_layout("epic-232", base_state_root=tmp_path)
    assert layout.state_root == tmp_path / "dev-runtimes" / "epic-232" / "state"
    assert layout.config_path == tmp_path / "dev-runtimes" / "epic-232" / "config.toml"
    assert layout.tunnel_id == "dev-epic-232"
    assert layout.profile == "dev-epic-232"
    assert layout.env() == {
        "REPOFORGE_TUNNEL_ID": "dev-epic-232",
        "REPOFORGE_TUNNEL_PROFILE": "dev-epic-232",
    }


def test_layout_rejects_unsafe_names(tmp_path: Path) -> None:
    for bad in ("../escape", "Has Space", "UPPER", ""):
        with pytest.raises(ConfigError, match="DEV_RUNTIME_NAME_INVALID"):
            derive_dev_runtime_layout(bad, base_state_root=tmp_path)


def test_record_path_is_namespaced_under_the_dev_state_root(tmp_path: Path) -> None:
    layout = derive_dev_runtime_layout("cand", base_state_root=tmp_path)
    record = dev_runtime_record_path(layout)
    # Lives under the dev state_root's config-locks namespace, never production's.
    assert layout.state_root in record.parents
    assert record.name == "managed-runtime-v3.json"


# ------------------------------------------------------------------------ provisioner


def test_flat_toml_round_trips_arbitrary_config_structure() -> None:
    document = tomllib.loads(_BASE_CONFIG)
    reparsed = tomllib.loads(render_flat_toml(document))
    assert reparsed == document


def test_provisioner_overrides_only_the_three_isolation_keys(tmp_path: Path) -> None:
    base = tmp_path / "config.toml"
    base.write_text(_BASE_CONFIG, encoding="utf-8")
    dev = tmp_path / "dev" / "config.toml"
    TomlDevConfigProvisioner().provision(
        base,
        dev,
        state_root=tmp_path / "dev" / "state",
        tunnel_id="dev-x",
        profile="dev-x",
    )
    result = tomllib.loads(dev.read_text(encoding="utf-8"))
    assert result["server"]["state_root"] == str(tmp_path / "dev" / "state")
    assert result["tunnel"] == {"id": "dev-x", "profile": "dev-x"}
    # Everything else is preserved verbatim.
    assert result["server"]["max_background_profiles"] == 2
    assert result["repo"] == [{"id": "app", "path": "/home/dev/app"}]
    assert result["repositories"]["app"]["profiles"]["full"]["commands"] == [["make", "test"]]


# ---------------------------------------------------------------------------- service


class _Launcher:
    def __init__(self) -> None:
        self.started: list[tuple[Path, bool, dict[str, str]]] = []

    def start(self, config_path: Path, *, foreground: bool, extra_env: dict[str, str]) -> int:
        self.started.append((config_path, foreground, extra_env))
        return 4242

    def force_stop(self, record: object, *, grace_seconds: float = 5.0) -> bool:
        return True


class _RecordingProvisioner:
    def __init__(self) -> None:
        self.calls: list[tuple[Path, Path, str]] = []

    def provision(self, base_config, dev_config, *, state_root, tunnel_id, profile) -> None:
        self.calls.append((base_config, dev_config, tunnel_id))
        dev_config.parent.mkdir(parents=True, exist_ok=True)
        dev_config.write_text("stub", encoding="utf-8")


class _NullStore:
    def read(self):
        return None

    def write(self, record) -> None:  # pragma: no cover - unused
        raise NotImplementedError

    def clear(self, *, expected_pid: int | None = None) -> None:  # pragma: no cover - unused
        raise NotImplementedError


def _service(
    tmp_path: Path, launcher: _Launcher, provisioner: _RecordingProvisioner
) -> DevRuntimeService:
    base = tmp_path / "config.toml"
    base.write_text(_BASE_CONFIG, encoding="utf-8")
    return DevRuntimeService(
        launcher=launcher,
        provisioner=provisioner,
        runtime_store_factory=lambda _path: _NullStore(),
        base_config=base,
        base_state_root=tmp_path / "state",
    )


def test_start_provisions_an_isolated_config_and_launches_with_the_dev_tunnel(
    tmp_path: Path,
) -> None:
    launcher = _Launcher()
    provisioner = _RecordingProvisioner()
    service = _service(tmp_path, launcher, provisioner)

    result = service.start("cand")

    assert result["status"] == "starting"
    assert result["tunnel_id"] == "dev-cand"
    assert provisioner.calls and provisioner.calls[0][2] == "dev-cand"
    # Launched against the dev config, in background, with the dev tunnel env.
    config_path, foreground, env = launcher.started[0]
    assert config_path == tmp_path / "state" / "dev-runtimes" / "cand" / "config.toml"
    assert foreground is False
    assert env["REPOFORGE_TUNNEL_ID"] == "dev-cand"


def test_stop_reports_not_running_when_no_record_exists(tmp_path: Path) -> None:
    service = _service(tmp_path, _Launcher(), _RecordingProvisioner())
    assert service.stop("cand")["status"] == "not_running"


def test_list_enumerates_started_dev_runtimes_with_their_state(tmp_path: Path) -> None:
    """A listing of bare names cannot answer "which one is running, which do I switch to".

    The names are still there, but each entry now carries the same live state ``status()``
    reports, so nobody has to call ``status`` once per name to make a decision.
    """
    launcher = _Launcher()
    service = _service(tmp_path, launcher, _RecordingProvisioner())
    service.start("alpha")
    service.start("beta")

    assert service.names() == ["alpha", "beta"]
    listed = service.list()["dev_runtimes"]
    assert isinstance(listed, list)
    assert [entry["name"] for entry in listed] == ["alpha", "beta"]
    for entry in listed:
        # The fields a reader actually needs to choose between runtimes.
        assert set(entry) >= {
            "name",
            "phase",
            "pid",
            "tunnel_id",
            "config_path",
            "state_root",
            "exists",
        }
        assert entry["tunnel_id"] == f"dev-{entry['name']}"
        assert entry["exists"] is True
