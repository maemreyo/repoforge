"""Coverage for zero-config rule loading (#204)."""

from __future__ import annotations

from pathlib import Path

import pytest

from repoforge.application.rules.loader import DEFAULT_RULES, load_rules
from repoforge.application.rules.validators import ReviewContext, run_review
from repoforge.domain.rules_engine import (
    RuleResultState,
    RuleValidationError,
    UnsupportedEnforcementError,
)


def test_load_rules_returns_defaults_when_repoforge_dir_is_absent(tmp_path: Path) -> None:
    rules = load_rules(tmp_path)
    assert rules == DEFAULT_RULES


def test_load_rules_returns_defaults_when_rules_dir_is_empty(tmp_path: Path) -> None:
    (tmp_path / ".repoforge" / "rules").mkdir(parents=True)
    assert load_rules(tmp_path) == DEFAULT_RULES


def test_zero_config_defaults_flag_an_oversized_file(tmp_path: Path) -> None:
    (tmp_path / "big.py").write_text("\n".join(str(i) for i in range(500)), encoding="utf-8")
    rules = load_rules(tmp_path)
    report = run_review(rules, ReviewContext(root=tmp_path))
    assert any(f.file == "big.py" and f.state is RuleResultState.FAIL for f in report.findings)


def test_repo_yaml_overrides_the_default_rule_by_id(tmp_path: Path) -> None:
    (tmp_path / "big.py").write_text("\n".join(str(i) for i in range(500)), encoding="utf-8")
    rules_dir = tmp_path / ".repoforge" / "rules"
    rules_dir.mkdir(parents=True)
    (rules_dir / "custom.yaml").write_text(
        "- id: default.file-length\n"
        "  enforcement: advisory\n"
        "  validator: file_length\n"
        "  paths: ['**/*.py']\n"
        "  max_lines: 10000\n",
        encoding="utf-8",
    )
    rules = load_rules(tmp_path)
    assert len(rules) == 1
    report = run_review(rules, ReviewContext(root=tmp_path))
    assert report.findings == ()


def test_load_rules_rejects_hard_enforcement(tmp_path: Path) -> None:
    rules_dir = tmp_path / ".repoforge" / "rules"
    rules_dir.mkdir(parents=True)
    (rules_dir / "bad.yaml").write_text(
        "- id: some.rule\n  enforcement: hard\n  validator: file_length\n  paths: ['**/*.py']\n",
        encoding="utf-8",
    )
    with pytest.raises(UnsupportedEnforcementError):
        load_rules(tmp_path)


def test_load_rules_rejects_unknown_validator(tmp_path: Path) -> None:
    rules_dir = tmp_path / ".repoforge" / "rules"
    rules_dir.mkdir(parents=True)
    (rules_dir / "bad.yaml").write_text(
        "- id: some.rule\n  validator: shell_exec\n  paths: ['**/*.py']\n",
        encoding="utf-8",
    )
    with pytest.raises(RuleValidationError):
        load_rules(tmp_path)


def test_load_rules_rejects_malformed_yaml(tmp_path: Path) -> None:
    rules_dir = tmp_path / ".repoforge" / "rules"
    rules_dir.mkdir(parents=True)
    (rules_dir / "bad.yaml").write_text("id: [unterminated\n", encoding="utf-8")
    with pytest.raises(RuleValidationError):
        load_rules(tmp_path)


def test_additional_custom_rule_is_added_alongside_defaults(tmp_path: Path) -> None:
    rules_dir = tmp_path / ".repoforge" / "rules"
    rules_dir.mkdir(parents=True)
    (rules_dir / "extra.yaml").write_text(
        "- id: application.no-adapter-imports\n"
        "  validator: import_boundary\n"
        "  paths: ['src/**/*.py']\n"
        "  forbid: ['adapters']\n"
        "  override_policy: task\n",
        encoding="utf-8",
    )
    rules = load_rules(tmp_path)
    ids = {rule.id for rule in rules}
    assert ids == {"default.file-length", "application.no-adapter-imports"}
