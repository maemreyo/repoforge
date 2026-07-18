"""Coverage for the idempotent `rf rules init` scaffold (#205)."""

from __future__ import annotations

import importlib
import json
import subprocess
from pathlib import Path

import pytest
from conftest import ForgeEnvironment

from repoforge.application.skills.scaffold import scaffold_repoforge_rules

cli = importlib.import_module("repoforge.interfaces.cli.main")


def test_scaffold_creates_starter_rules_and_skills_yaml(tmp_path: Path) -> None:
    created = scaffold_repoforge_rules(tmp_path)
    assert set(created) == {".repoforge/rules/example.yaml", ".repoforge/skills.yaml"}
    assert (tmp_path / ".repoforge" / "rules" / "example.yaml").is_file()
    assert (tmp_path / ".repoforge" / "skills.yaml").is_file()


def test_running_twice_produces_no_diff(tmp_path: Path) -> None:
    scaffold_repoforge_rules(tmp_path)
    rules_before = (tmp_path / ".repoforge" / "rules" / "example.yaml").read_text(encoding="utf-8")
    skills_before = (tmp_path / ".repoforge" / "skills.yaml").read_text(encoding="utf-8")

    second_run_created = scaffold_repoforge_rules(tmp_path)

    assert second_run_created == ()
    assert (tmp_path / ".repoforge" / "rules" / "example.yaml").read_text(
        encoding="utf-8"
    ) == rules_before
    assert (tmp_path / ".repoforge" / "skills.yaml").read_text(encoding="utf-8") == skills_before


def test_scaffold_does_not_overwrite_a_customized_starter_file(tmp_path: Path) -> None:
    scaffold_repoforge_rules(tmp_path)
    example = tmp_path / ".repoforge" / "rules" / "example.yaml"
    example.write_text(
        "- id: custom.rule\n  validator: file_length\n  paths: ['**/*.py']\n", encoding="utf-8"
    )

    scaffold_repoforge_rules(tmp_path)  # must not clobber the operator's edit
    assert "custom.rule" in example.read_text(encoding="utf-8")


def test_scaffolded_rules_file_loads_cleanly_as_zero_active_rules(tmp_path: Path) -> None:
    from repoforge.application.rules.loader import DEFAULT_RULES, load_rules

    scaffold_repoforge_rules(tmp_path)
    # An all-commented starter file parses to nothing active; defaults still apply.
    assert load_rules(tmp_path) == DEFAULT_RULES


def test_cli_rules_init_scaffolds_and_leaves_git_index_untouched(
    forge_env: ForgeEnvironment, capsys: pytest.CaptureFixture[str]
) -> None:
    status_before = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=forge_env.source,
        capture_output=True,
        text=True,
        check=True,
    ).stdout

    assert (
        cli.main(["--config", str(forge_env.config_path), "rules", "init", "--repo-id", "demo"])
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert ".repoforge/rules/example.yaml" in payload["created"]

    status_after = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=forge_env.source,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    # New files exist on disk (git sees them as untracked) but nothing was staged or committed.
    assert "A  " not in status_after
    assert status_before != status_after  # untracked files now visible
    assert all(line.startswith("??") for line in status_after.splitlines() if line.strip())
