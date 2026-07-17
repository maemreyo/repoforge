from pathlib import Path

import pytest

from repoforge.config import DEFAULT_DENIED_PATHS, load_config
from repoforge.domain.errors import ConfigError
from repoforge.domain.verification_steps import no_regression_receipt


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
    assert [step.step_id for step in repository.profiles["test"].steps] == ["step-1"]
    assert repository.profiles["test"].steps[0].kind.value == "unknown"
    assert repository.risk_policy.final_profile == "test"
    assert repository.risk_policy.ordered_profiles[-1] == "test"
    assert loaded.server.workspace_root == (tmp_path / "workspaces").resolve()
    assert repository.denied_paths == DEFAULT_DENIED_PATHS


def test_load_structured_profile_steps_and_no_regression_policy(tmp_path: Path) -> None:
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
baseline_policy = "no_regression"
commands = [
  ["ruff", "format", "--check", "."],
  ["pytest", "-q"],
]

[[repositories.demo.profiles.full.steps]]
id = "format"
kind = "hygiene"
command = ["ruff", "format", "--check", "."]

[[repositories.demo.profiles.full.steps]]
id = "tests"
kind = "business_tests"
command = ["pytest", "-q"]
""",
        encoding="utf-8",
    )

    profile = load_config(config).repositories["demo"].profiles["full"]

    assert profile.baseline_policy.value == "no_regression"
    assert [(step.step_id, step.kind.value) for step in profile.steps] == [
        ("format", "hygiene"),
        ("tests", "business_tests"),
    ]
    assert profile.commands == tuple(step.command for step in profile.steps)


def test_structured_profile_rejects_commands_that_disagree_with_steps(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    config = tmp_path / "config.toml"
    config.write_text(
        f"""[repositories.demo]
path = "{repo}"

[repositories.demo.profiles.full]
verification = true
commands = [["pytest", "-q"]]

[[repositories.demo.profiles.full.steps]]
id = "tests"
kind = "business_tests"
command = ["pytest", "-x"]
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="must match steps"):
        load_config(config)


def test_no_regression_hygiene_receipt_requires_clean_changed_paths() -> None:
    receipt = no_regression_receipt(
        base_sha="a" * 40,
        workspace_fingerprint="b" * 64,
        formatter_contract_hash="c" * 64,
        environment_identity="env-1",
        preexisting_count=3,
        introduced_count=0,
        changed_path_finding_count=0,
        output_truncated=False,
    )

    assert receipt is not None
    assert receipt.preexisting_count == 3
    assert (
        no_regression_receipt(
            base_sha="a" * 40,
            workspace_fingerprint="b" * 64,
            formatter_contract_hash="c" * 64,
            environment_identity="env-1",
            preexisting_count=3,
            introduced_count=1,
            changed_path_finding_count=0,
            output_truncated=False,
        )
        is None
    )


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
