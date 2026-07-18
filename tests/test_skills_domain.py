"""Pure domain coverage for the skill schema and collision resolution (#205)."""

from __future__ import annotations

from repoforge.domain.skills import Skill, SkillRootKind, resolve_collisions, root_rank


def _skill(name: str, kind: SkillRootKind, path: str) -> Skill:
    return Skill(name=name, description="d", root_kind=kind, path=path, digest="x" * 64)


def test_root_rank_orders_agents_before_claude_before_legacy_before_user() -> None:
    assert root_rank(SkillRootKind.AGENTS) < root_rank(SkillRootKind.CLAUDE)
    assert root_rank(SkillRootKind.CLAUDE) < root_rank(SkillRootKind.AGENT_LEGACY)
    assert root_rank(SkillRootKind.AGENT_LEGACY) < root_rank(SkillRootKind.USER)


def test_no_collision_when_all_names_are_unique() -> None:
    skills = (
        _skill("a", SkillRootKind.AGENTS, ".agents/skills/a"),
        _skill("b", SkillRootKind.CLAUDE, ".claude/skills/b"),
    )
    catalog = resolve_collisions(skills)
    assert catalog.collisions == ()
    assert {s.name for s in catalog.skills} == {"a", "b"}


def test_agents_root_wins_over_claude_root_deterministically() -> None:
    skills = (
        _skill("dup", SkillRootKind.CLAUDE, ".claude/skills/dup"),
        _skill("dup", SkillRootKind.AGENTS, ".agents/skills/dup"),
    )
    catalog = resolve_collisions(skills)
    winner = catalog.get("dup")
    assert winner is not None
    assert winner.root_kind is SkillRootKind.AGENTS
    assert len(catalog.collisions) == 1
    collision = catalog.collisions[0]
    assert collision.winner_path == ".agents/skills/dup"
    assert collision.shadowed_paths == (".claude/skills/dup",)


def test_repo_root_always_wins_over_user_root() -> None:
    skills = (
        _skill("dup", SkillRootKind.USER, "/home/user/.claude/skills/dup"),
        _skill("dup", SkillRootKind.AGENT_LEGACY, ".agent/skills/dup"),
    )
    catalog = resolve_collisions(skills)
    assert catalog.get("dup").root_kind is SkillRootKind.AGENT_LEGACY  # type: ignore[union-attr]


def test_collision_resolution_is_independent_of_input_order() -> None:
    a = _skill("dup", SkillRootKind.CLAUDE, ".claude/skills/dup")
    b = _skill("dup", SkillRootKind.AGENTS, ".agents/skills/dup")
    assert resolve_collisions((a, b)).skills == resolve_collisions((b, a)).skills
