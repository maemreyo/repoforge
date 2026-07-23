from pathlib import Path

import pytest

from repoforge.config import DEFAULT_DENIED_PATHS, load_config
from repoforge.domain.errors import ConfigError
from repoforge.domain.issue_writes import IssueWritePolicy
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
    assert repository.issue_writes.enabled_ops == ("comment",)
    assert repository.issue_writes.approval_required_ops == ()
    assert repository.issue_writes.max_writes_per_call == 2


def test_load_typed_issue_write_policy(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    config = tmp_path / "config.toml"
    config.write_text(
        f"""[repositories.demo]
path = "{repo}"

[repositories.demo.issue_writes]
enabled_ops = ["comment", "close", "create"]
approval_required_ops = ["close"]
max_writes_per_call = 3
max_writes_per_window = 12
window_seconds = 900
create_title_prefix = "[FOLLOWUP]"
create_body_template = "## Objective\\n{{body}}\\n\\n## Evidence\\n{{evidence_ref}}"
""",
        encoding="utf-8",
    )

    policy = load_config(config).repositories["demo"].issue_writes

    assert policy.enabled_ops == ("comment", "close", "create")
    assert policy.approval_required_ops == ("close",)
    assert policy.max_writes_per_call == 3
    assert policy.max_writes_per_window == 12
    assert policy.window_seconds == 900
    assert policy.create_title_prefix == "[FOLLOWUP]"


def test_issue_write_policy_versions_update_authority_without_breaking_legacy_configs() -> None:
    legacy = IssueWritePolicy.from_table(
        {"enabled_ops": ["create"], "approval_required_ops": ["create"]},
        context="repositories.demo.issue_writes",
    )
    current = IssueWritePolicy.from_table(
        {
            "operation_semantics_version": 2,
            "enabled_ops": ["create"],
            "approval_required_ops": ["create"],
        },
        context="repositories.demo.issue_writes",
    )
    current_with_update = IssueWritePolicy.from_table(
        {
            "operation_semantics_version": 2,
            "enabled_ops": ["update"],
            "approval_required_ops": ["update"],
        },
        context="repositories.demo.issue_writes",
    )

    assert legacy.operation_semantics_version == 1
    assert legacy.allows_effect("update") is True
    assert legacy.requires_effect_approval("update") is True
    assert current.operation_semantics_version == 2
    assert current.allows_effect("update") is False
    assert current.requires_effect_approval("update") is False
    assert current_with_update.allows_effect("update") is True
    assert current_with_update.requires_effect_approval("update") is True
    assert current_with_update.as_table()["operation_semantics_version"] == 2


def test_issue_write_approval_ops_must_be_enabled(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    config = tmp_path / "config.toml"
    config.write_text(
        f"""[repositories.demo]
path = "{repo}"

[repositories.demo.issue_writes]
enabled_ops = ["comment"]
approval_required_ops = ["close"]
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="approval_required_ops"):
        load_config(config)


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
