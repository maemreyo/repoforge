"""Coverage for skill selection tiers: binding primary, lexical auto-match fallback (#205)."""

from __future__ import annotations

from repoforge.application.skills.binding import SkillBinding
from repoforge.application.skills.selection import select_skills
from repoforge.domain.delivery import DeliveryClass
from repoforge.domain.skills import Skill, SkillCatalog, SkillRootKind


def _skill(name: str, description: str) -> Skill:
    return Skill(
        name=name,
        description=description,
        root_kind=SkillRootKind.CLAUDE,
        path=name,
        digest="x" * 64,
    )


def test_binding_always_wins_regardless_of_lexical_overlap() -> None:
    catalog = SkillCatalog(skills=(_skill("tdd", "unrelated text"),), collisions=())
    bindings = (SkillBinding(skill="tdd", delivery=DeliveryClass.ALWAYS),)
    selected = select_skills(catalog, bindings, intent="fix the bug")
    assert len(selected) == 1
    assert selected[0].skill == "tdd"
    assert selected[0].delivery is DeliveryClass.ALWAYS
    assert selected[0].reason == "binding"


def test_auto_match_surfaces_top_candidates_by_keyword_overlap() -> None:
    catalog = SkillCatalog(
        skills=(
            _skill("tdd", "test driven development red green refactor cycle"),
            _skill("docx", "generate word documents and letters"),
            _skill("unrelated", "completely different topic about weather"),
        ),
        collisions=(),
    )
    selected = select_skills(catalog, (), intent="I want to do test driven development refactor")
    reasons = {s.skill: s.reason for s in selected}
    assert reasons.get("tdd") == "auto_match"
    assert "unrelated" not in reasons


def test_no_query_words_yields_no_auto_match_candidates() -> None:
    catalog = SkillCatalog(skills=(_skill("tdd", "test driven development"),), collisions=())
    selected = select_skills(catalog, ())
    assert selected == ()


def test_auto_match_never_duplicates_an_already_bound_skill() -> None:
    catalog = SkillCatalog(skills=(_skill("tdd", "test driven development"),), collisions=())
    bindings = (SkillBinding(skill="tdd"),)
    selected = select_skills(catalog, bindings, intent="test driven development")
    assert len(selected) == 1
    assert selected[0].reason == "binding"


def test_at_most_two_auto_match_candidates() -> None:
    catalog = SkillCatalog(
        skills=tuple(_skill(f"s{i}", "shared keyword topic") for i in range(5)),
        collisions=(),
    )
    selected = select_skills(catalog, (), intent="shared keyword topic")
    assert len(selected) == 2
