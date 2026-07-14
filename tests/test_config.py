from pathlib import Path

import pytest

from repoforge.config import DEFAULT_DENIED_PATHS, load_config
from repoforge.domain.errors import ConfigError


def test_load_minimal_config(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    config = tmp_path / "config.toml"
    config.write_text(
        f"""[server]
workspace_root = "{tmp_path / "workspaces"}"
state_root = "{tmp_path / "state"}"

[repositories.demo]
path = "{repo}"
default_base = "main"
allowed_base_branches = ["main"]
branch_prefix = "ai/"

[repositories.demo.profiles.test]
verification = true
commands = [["python", "-m", "pytest"]]
""",
        encoding="utf-8",
    )
    loaded = load_config(config)
    repository = loaded.repositories["demo"]
    assert repository.profiles["test"].verification is True
    assert repository.risk_policy.final_profile == "test"
    assert repository.risk_policy.ordered_profiles[-1] == "test"
    assert loaded.server.workspace_root == (tmp_path / "workspaces").resolve()
    assert repository.denied_paths == DEFAULT_DENIED_PATHS


def test_unsafe_remote_name_is_rejected(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    config = tmp_path / "config.toml"
    config.write_text(
        f"""[repositories.demo]
path = "{repo}"
remote = "--upload-pack=evil"
""",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="remote"):
        load_config(config)


def test_invalid_risk_threshold_order_is_rejected(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    config = tmp_path / "config.toml"
    config.write_text(
        f"""[repositories.demo]
path = "{repo}"
default_verification_profile = "full"

[repositories.demo.profiles.full]
verification = true
commands = [["python", "-m", "pytest"]]

[repositories.demo.risk]
low_max = 60
medium_max = 40
high_max = 80
final_profile = "full"
ordered_profiles = ["full"]
narrow_diagnostics = []
""",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="thresholds"):
        load_config(config)
