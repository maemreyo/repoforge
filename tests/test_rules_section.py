"""`repo_task_context` section `rules`: the typed rules in force, end to end.

First section of #206 wired all the way through — the accepted-constitution compile (#354)
reaching a caller. The guard that matters is the same one #354 tests at the unit level,
asserted here through the public read instead: a task editing `.repoforge/rules/` is judged
by the accepted constitution, not by its own uncommitted edits.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import ForgeEnvironment, git

from repoforge.domain.context_sections import (
    DEFAULT_CONTEXT_SECTION_VALUES,
    ContextSectionName,
)
from repoforge.domain.errors import ErrorCode, RepoForgeError

_ACCEPTED_RULE = """\
- id: demo.file-length
  enforcement: checked
  validator: file_length
  paths: ["**/*.py"]
  max_lines: 400
"""

_WIDENED_RULE = """\
- id: demo.file-length
  enforcement: checked
  validator: file_length
  paths: ["**/*.py"]
  max_lines: 99999
"""


def _write(root: Path, relative: str, content: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _commit(source: Path, message: str) -> None:
    git("add", "-A", cwd=source)
    git("commit", "-m", message, cwd=source)


def _rules_facts(forge_env: ForgeEnvironment) -> dict[str, str]:
    result = forge_env.service.repo_task_context_v2("demo", sections=["rules"])
    (section,) = result["sections"]
    assert section["name"] == "rules"
    return {fact["key"]: fact["value"] for fact in section["facts"]}


# --------------------------------------------------------------------- the guard


def test_the_rules_section_reports_the_accepted_constitution_not_the_worktree(
    forge_env: ForgeEnvironment,
) -> None:
    _write(forge_env.source, ".repoforge/rules/limits.yaml", _ACCEPTED_RULE)
    _commit(forge_env.source, "accept a file-length rule")

    # A task now proposes widening it, uncommitted, in the tree it is working in.
    _write(forge_env.source, ".repoforge/rules/limits.yaml", _WIDENED_RULE)

    facts = _rules_facts(forge_env)
    rule = json.loads(facts["demo.file-length"])

    assert rule["params"]["max_lines"] == 400, (
        "the section must report the accepted rule, not the task's own proposal"
    )


def test_accepting_a_change_moves_both_the_rule_and_the_constitution_sha(
    forge_env: ForgeEnvironment,
) -> None:
    _write(forge_env.source, ".repoforge/rules/limits.yaml", _ACCEPTED_RULE)
    _commit(forge_env.source, "accept a rule")
    before = _rules_facts(forge_env)

    _write(forge_env.source, ".repoforge/rules/limits.yaml", _WIDENED_RULE)
    _commit(forge_env.source, "accept the widened rule")
    after = _rules_facts(forge_env)

    assert before["constitution_sha"] != after["constitution_sha"]
    assert json.loads(after["demo.file-length"])["params"]["max_lines"] == 99999


# ------------------------------------------------------------------- the contents


def test_the_section_names_which_constitution_it_read(forge_env: ForgeEnvironment) -> None:
    _write(forge_env.source, ".repoforge/rules/limits.yaml", _ACCEPTED_RULE)
    _commit(forge_env.source, "accept a rule")

    facts = _rules_facts(forge_env)

    assert len(facts["constitution_sha"]) == 64
    assert facts["constitution_ref"] == "refs/heads/main"
    assert len(facts["constitution_commit"]) == 40
    # One fact per rule, so the byte budget trims the list rather than the identity.
    assert int(facts["rule_count"]) >= 1
    assert "demo.file-length" in facts


def test_a_repository_with_no_rules_reports_the_conservative_defaults(
    forge_env: ForgeEnvironment,
) -> None:
    """Zero-config is not the same as ungoverned."""

    facts = _rules_facts(forge_env)

    assert "default.file-length" in facts
    assert int(facts["rule_count"]) >= 1


def test_an_unreadable_constitution_is_unavailable_not_an_empty_rule_set(
    forge_env: ForgeEnvironment,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ "No rules" and "could not find out" are different answers.

    Reported as an empty rule set, an agent would proceed believing nothing governs it.
    """

    def refuse(*_args: object, **_kwargs: object) -> object:
        raise RepoForgeError("ref unavailable", code=ErrorCode.REPOSITORY_REF_DISALLOWED)

    monkeypatch.setattr(forge_env.service.application.context.git, "resolve_snapshot_ref", refuse)

    result = forge_env.service.repo_task_context_v2("demo", sections=["rules"])
    (section,) = result["sections"]

    assert section["freshness"] == "unavailable"
    assert section["complete"] is False
    keys = {fact["key"] for fact in section["facts"]}
    assert keys == {"unavailable_reason"}
    assert "constitution_sha" not in keys


# -------------------------------------------------------------------- the default set


def test_rules_is_not_in_the_default_section_set() -> None:
    """It costs a git snapshot read, so a caller opts in rather than paying by accident."""

    assert ContextSectionName.RULES.value not in DEFAULT_CONTEXT_SECTION_VALUES
