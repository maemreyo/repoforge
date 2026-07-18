"""Skill selection tiers (#205): binding (primary) -> lexical auto-match (fallback) -> catalog
(net). No embeddings in V1 -- auto-match is a bounded keyword-overlap score, good enough to
prove a binding-free repo still surfaces relevant candidates, never good enough to be trusted
as the sole selection mechanism for something safety-relevant (that's what bindings are for).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ...domain.delivery import DeliveryClass
from ...domain.skills import SkillCatalog
from .binding import SkillBinding

_WORD = re.compile(r"[a-zA-Z][a-zA-Z0-9_-]{2,}")
_MAX_AUTO_MATCH_CANDIDATES = 2


@dataclass(frozen=True, slots=True)
class SelectedSkill:
    skill: str
    delivery: DeliveryClass
    reason: str  # "binding" | "auto_match"


def _words(text: str) -> set[str]:
    return {match.group(0).lower() for match in _WORD.finditer(text)}


def select_skills(
    catalog: SkillCatalog,
    bindings: tuple[SkillBinding, ...],
    *,
    intent: str = "",
    focus_paths: tuple[str, ...] = (),
) -> tuple[SelectedSkill, ...]:
    bound_names = {binding.skill for binding in bindings}
    selected: list[SelectedSkill] = [
        SelectedSkill(binding.skill, binding.delivery, "binding")
        for binding in bindings
        if catalog.get(binding.skill) is not None
    ]

    query_words = _words(intent) | {word for path in focus_paths for word in _words(path)}
    if not query_words:
        return tuple(selected)

    scored: list[tuple[int, str]] = []
    for skill in catalog.skills:
        if skill.name in bound_names:
            continue
        overlap = len(query_words & _words(skill.description))
        if overlap > 0:
            scored.append((overlap, skill.name))
    scored.sort(key=lambda item: (-item[0], item[1]))
    for _, name in scored[:_MAX_AUTO_MATCH_CANDIDATES]:
        selected.append(SelectedSkill(name, DeliveryClass.ON_ENTRY, "auto_match"))
    return tuple(selected)
