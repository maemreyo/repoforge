"""Typed skill schema (#205): a skill is a directory carrying SKILL.md plus inert reference
content. Ingestion never grants execution capability -- `scripts/` is indexed as text, never
wired into any command runner. Selection binding lives alongside these types so both #205
(skills) and #204 (rules) share one delivery vocabulary (domain/delivery.py).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class SkillRootKind(str, Enum):
    """Deterministic precedence, highest first: repo roots beat user roots, and among repo
    roots the Codex-standard directory beats the two compatibility/legacy locations."""

    AGENTS = "agents_skills"  # .agents/skills/  (Codex standard)
    CLAUDE = "claude_skills"  # .claude/skills/  (compatibility)
    AGENT_LEGACY = "agent_skills"  # .agent/skills/   (legacy)
    USER = "user_skills"  # configured user-level root, read-only


_ROOT_PRECEDENCE: dict[SkillRootKind, int] = {
    SkillRootKind.AGENTS: 0,
    SkillRootKind.CLAUDE: 1,
    SkillRootKind.AGENT_LEGACY: 2,
    SkillRootKind.USER: 3,
}


def root_rank(kind: SkillRootKind) -> int:
    """Lower rank wins. Repo roots (0-2) always beat user roots (3)."""
    return _ROOT_PRECEDENCE[kind]


@dataclass(frozen=True, slots=True)
class Skill:
    name: str
    description: str
    root_kind: SkillRootKind
    path: str
    digest: str
    scripts: tuple[str, ...] = ()
    upstream: str | None = None

    def as_catalog_entry(self) -> dict[str, object]:
        return {"name": self.name, "description": self.description}


@dataclass(frozen=True, slots=True)
class SkillCollision:
    name: str
    winner_path: str
    shadowed_paths: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "winner_path": self.winner_path,
            "shadowed_paths": list(self.shadowed_paths),
        }


@dataclass(frozen=True, slots=True)
class SkillCatalog:
    skills: tuple[Skill, ...]
    collisions: tuple[SkillCollision, ...]

    def get(self, name: str) -> Skill | None:
        for skill in self.skills:
            if skill.name == name:
                return skill
        return None

    def as_dict(self) -> dict[str, object]:
        return {
            "skills": [skill.as_catalog_entry() for skill in self.skills],
            "collisions": [collision.as_dict() for collision in self.collisions],
        }


def resolve_collisions(skills: tuple[Skill, ...]) -> SkillCatalog:
    """Deterministic winner per skill name: lowest root_rank wins; ties break on path so the
    result never depends on filesystem iteration order. Every collision is reported, never
    silently shadowed.
    """

    by_name: dict[str, list[Skill]] = {}
    for skill in skills:
        by_name.setdefault(skill.name, []).append(skill)

    winners: list[Skill] = []
    collisions: list[SkillCollision] = []
    for name in sorted(by_name):
        candidates = sorted(by_name[name], key=lambda s: (root_rank(s.root_kind), s.path))
        winners.append(candidates[0])
        if len(candidates) > 1:
            collisions.append(
                SkillCollision(
                    name=name,
                    winner_path=candidates[0].path,
                    shadowed_paths=tuple(candidate.path for candidate in candidates[1:]),
                )
            )
    winners.sort(key=lambda s: s.name)
    return SkillCatalog(skills=tuple(winners), collisions=tuple(collisions))
