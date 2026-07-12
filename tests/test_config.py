from pathlib import Path

import pytest

from repoforge.config import DEFAULT_DENIED_PATHS, load_config
from repoforge.errors import ConfigError


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
    assert loaded.repositories["demo"].profiles["test"].verification is True
    assert loaded.server.workspace_root == (tmp_path / "workspaces").resolve()
    assert loaded.repositories["demo"].denied_paths == DEFAULT_DENIED_PATHS


def test_load_compact_multi_repository_config(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    for repo in (first, second):
        repo.mkdir()
        (repo / ".git").mkdir()

    config = tmp_path / "config.toml"
    config.write_text(
        f"""[repositories.first]
path = "{first}"

[repositories.first.actions]
setup = ["python", "-m", "pip", "install", "-e", "."]

[repositories.first.checks]
quick = [
  ["python", "-m", "ruff", "check", "."],
  ["python", "-m", "mypy", "."],
]
full = ["python", "-m", "pytest", "-q"]

[repositories.second]
path = "{second}"

[repositories.second.checks]
test = ["go", "test", "./..."]
""",
        encoding="utf-8",
    )

    loaded = load_config(config)
    assert sorted(loaded.repositories) == ["first", "second"]
    assert loaded.repositories["first"].profiles["setup"].verification is False
    assert loaded.repositories["first"].profiles["quick"].commands == (
        ("python", "-m", "ruff", "check", "."),
        ("python", "-m", "mypy", "."),
    )
    assert loaded.repositories["first"].default_verification_profile == "full"
    assert loaded.repositories["second"].default_verification_profile == "test"


def test_duplicate_compact_and_advanced_profiles_are_rejected(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    config = tmp_path / "config.toml"
    config.write_text(
        f"""[repositories.demo]
path = "{repo}"

[repositories.demo.checks]
full = ["python", "-m", "pytest"]

[repositories.demo.profiles.full]
verification = true
commands = [["python", "-m", "pytest", "-q"]]
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="duplicate profiles"):
        load_config(config)


def test_compact_profile_rejects_shell_strings(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    config = tmp_path / "config.toml"
    config.write_text(
        f"""[repositories.demo]
path = "{repo}"

[repositories.demo.checks]
full = "python -m pytest"
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="non-empty command array"):
        load_config(config)


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
