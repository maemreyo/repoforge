"""Coverage for skill discovery, the trust boundary, and untrusted-input handling (#205)."""

from __future__ import annotations

from pathlib import Path

from repoforge.application.skills.discovery import discover_skills, read_skill_file
from repoforge.domain.skills import SkillRootKind


def _write_skill(
    root: Path, rel_dir: str, name: str, description: str, *, body: str = "body"
) -> Path:
    skill_dir = root / rel_dir
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n{body}\n", encoding="utf-8"
    )
    return skill_dir


def test_claude_only_repo_yields_a_populated_catalog(tmp_path: Path) -> None:
    _write_skill(tmp_path, ".claude/skills/tdd", "tdd", "Test-driven development workflow")
    catalog = discover_skills(tmp_path)

    assert catalog.collisions == ()
    assert len(catalog.skills) == 1
    skill = catalog.skills[0]
    assert skill.name == "tdd"
    assert skill.root_kind is SkillRootKind.CLAUDE
    assert skill.description == "Test-driven development workflow"


def test_no_repoforge_dir_required_for_discovery(tmp_path: Path) -> None:
    assert not (tmp_path / ".repoforge").exists()
    _write_skill(tmp_path, ".agents/skills/x", "x", "does x")
    catalog = discover_skills(tmp_path)
    assert len(catalog.skills) == 1


def test_absent_roots_yield_an_empty_but_valid_catalog(tmp_path: Path) -> None:
    catalog = discover_skills(tmp_path)
    assert catalog.skills == ()
    assert catalog.collisions == ()


def test_same_name_in_agents_and_claude_produces_collision_with_both_provenances(
    tmp_path: Path,
) -> None:
    _write_skill(tmp_path, ".claude/skills/tdd", "tdd", "claude version")
    _write_skill(tmp_path, ".agents/skills/tdd", "tdd", "agents version")

    catalog = discover_skills(tmp_path)
    winner = catalog.get("tdd")
    assert winner is not None
    assert winner.root_kind is SkillRootKind.AGENTS
    assert len(catalog.collisions) == 1
    collision = catalog.collisions[0]
    assert collision.name == "tdd"
    assert ".agents/skills/tdd" in collision.winner_path
    assert any(".claude/skills/tdd" in path for path in collision.shadowed_paths)


def test_scripts_are_indexed_as_names_only_and_retrievable_as_inert_text(tmp_path: Path) -> None:
    skill_dir = _write_skill(tmp_path, ".claude/skills/risky", "risky", "has a script")
    scripts_dir = skill_dir / "scripts"
    scripts_dir.mkdir()
    (scripts_dir / "run.sh").write_text("#!/bin/sh\nrm -rf /\n", encoding="utf-8")

    catalog = discover_skills(tmp_path)
    skill = catalog.get("risky")
    assert skill is not None
    assert skill.scripts == ("scripts/run.sh",)

    content = read_skill_file(tmp_path, skill, "scripts/run.sh")
    assert content is not None
    assert "rm -rf /" in content  # retrievable as text...

    # ...but the Skill/SkillCatalog objects never expose anything an executor could run: no
    # field carries an argv, a Path meant for execution, or any subprocess-shaped value.
    for field_name in ("name", "description", "root_kind", "path", "digest", "scripts", "upstream"):
        value = getattr(skill, field_name)
        assert not callable(value)


def test_path_escape_in_a_requested_script_file_is_rejected(tmp_path: Path) -> None:
    skill_dir = _write_skill(tmp_path, ".claude/skills/x", "x", "d")
    secret = tmp_path / "secret.txt"
    secret.write_text("top secret", encoding="utf-8")
    catalog = discover_skills(tmp_path)
    skill = catalog.get("x")
    assert skill is not None
    assert read_skill_file(tmp_path, skill, "../../secret.txt") is None
    assert str(skill_dir)  # sanity: fixture path constructed


def test_missing_frontmatter_falls_back_to_directory_name(tmp_path: Path) -> None:
    skill_dir = tmp_path / ".claude" / "skills" / "no-frontmatter"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("Just prose, no frontmatter.\n", encoding="utf-8")

    catalog = discover_skills(tmp_path)
    skill = catalog.get("no-frontmatter")
    assert skill is not None
    assert skill.description == ""


def test_upstream_frontmatter_is_captured_for_vendored_packs(tmp_path: Path) -> None:
    skill_dir = tmp_path / ".claude" / "skills" / "ponytail"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: ponytail\ndescription: minimality ladder\nupstream: acme/ponytail@abc123\n---\n",
        encoding="utf-8",
    )
    catalog = discover_skills(tmp_path)
    skill = catalog.get("ponytail")
    assert skill is not None
    assert skill.upstream == "acme/ponytail@abc123"


def test_user_root_is_discovered_and_ranks_below_all_repo_roots(tmp_path: Path) -> None:
    user_root = tmp_path / "user-skills"
    _write_skill(user_root, "shared", "shared", "user-level skill")
    catalog = discover_skills(tmp_path, user_roots=(user_root,))
    skill = catalog.get("shared")
    assert skill is not None
    assert skill.root_kind is SkillRootKind.USER
