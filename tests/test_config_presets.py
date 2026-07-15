from pathlib import Path

import pytest

from repoforge.config import DEFAULT_DENIED_PATHS, load_config, policy_preset_reference
from repoforge.domain.errors import ConfigError


def _config(path: Path, repo: Path, policy: str | None = None, extra: str = "") -> None:
    policy_line = f'policy = "{policy}"\n' if policy else ""
    path.write_text(
        f"""[repositories.demo]
path = "{repo}"
{policy_line}{extra}""",
        encoding="utf-8",
    )


def test_policy_preset_reference_is_stable_and_reviewable() -> None:
    assert policy_preset_reference() == (
        ("strict", True, False, 25, 2_000, 5 * 1024 * 1024),
        ("standard", False, False, 75, 6_000, 10 * 1024 * 1024),
        ("relaxed", False, True, 150, 12_000, 25 * 1024 * 1024),
    )


def test_minimal_repository_config_resolves_to_fail_closed_strict_policy(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    config = tmp_path / "config.toml"
    _config(config, repo)

    loaded = load_config(config)
    resolved = loaded.repositories["demo"]

    assert resolved.read_only is True
    assert resolved.publish_enabled is False
    assert resolved.require_verification_before_commit is True
    assert resolved.fetch_before_workspace is False
    assert resolved.denied_paths == DEFAULT_DENIED_PATHS


@pytest.mark.parametrize(
    ("policy", "read_only", "publish_enabled", "max_diff_lines"),
    [
        ("strict", True, False, 2_000),
        ("standard", False, False, 6_000),
        ("relaxed", False, True, 12_000),
    ],
)
def test_named_policy_preset_resolves_documented_values(
    tmp_path: Path,
    policy: str,
    read_only: bool,
    publish_enabled: bool,
    max_diff_lines: int,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    config = tmp_path / "config.toml"
    _config(config, repo, policy)

    resolved = load_config(config).repositories["demo"]

    assert resolved.read_only is read_only
    assert resolved.publish_enabled is publish_enabled
    assert resolved.max_diff_lines == max_diff_lines
    assert resolved.protected_branches == ("main", "master", "develop", "production")
    assert resolved.denied_paths == DEFAULT_DENIED_PATHS


def test_explicit_repository_knob_overrides_named_policy_preset(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    config = tmp_path / "config.toml"
    _config(config, repo, "strict", "max_diff_lines = 5000\npublish_enabled = true\n")

    resolved = load_config(config).repositories["demo"]

    assert resolved.max_diff_lines == 5_000
    assert resolved.publish_enabled is True
    assert resolved.read_only is True


def test_unknown_policy_preset_names_allowed_values(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    config = tmp_path / "config.toml"
    _config(config, repo, "permissive")

    with pytest.raises(ConfigError, match=r"strict.*standard.*relaxed"):
        load_config(config)


def test_existing_full_form_config_resolves_without_preset_regression(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    config = tmp_path / "config.toml"
    _config(
        config,
        repo,
        extra="""display_name = "Demo"
remote = "origin"
default_base = "main"
allowed_base_branches = ["main"]
branch_prefix = "ai/"
protected_branches = ["main", "master"]
read_only = false
publish_enabled = true
require_verification_before_commit = true
fetch_before_workspace = true
max_changed_files = 150
max_diff_lines = 12000
max_total_changed_bytes = 26214400
allowed_paths = []
denied_paths = [".git"]
pr_labels = []
pr_reviewers = []
no_maintainer_edit = false
""",
    )

    resolved = load_config(config).repositories["demo"]

    assert resolved.display_name == "Demo"
    assert resolved.publish_enabled is True
    assert resolved.max_changed_files == 150
    assert resolved.denied_paths == (".git",)
