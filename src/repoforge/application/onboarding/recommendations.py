"""Fail-closed recommendations for repetitive onboarding decisions."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class DecisionRecommendation:
    code: str
    value: str
    rationale: str


_SAFE_DEFAULTS: dict[str, tuple[str, str]] = {
    "publishing_access": (
        "local_only",
        "keep remote publishing disabled while preserving local work",
    ),
    "dependency_install": ("exclude", "avoid networked dependency setup"),
    "autofix": ("exclude", "avoid repository-mutating autofix commands"),
    "risky_commands": (
        "exclude",
        "keep deploy, release, and destructive commands unavailable",
    ),
    "submodules": ("read_only_parent", "keep submodule content outside writable scope"),
    "lfs": ("read_only", "keep Git LFS content outside writable scope"),
    "repository_budget": ("keep_defaults", "retain bounded default change budgets"),
    "existing_policy": (
        "preserve_read_only",
        "avoid replacing existing policy without explicit review",
    ),
    "existing_worktrees": (
        "use_new_isolated",
        "never reuse or mutate an existing worktree",
    ),
}


def recommend_safe_decisions(
    required_decisions: tuple[tuple[str, str, tuple[str, ...]], ...],
) -> tuple[DecisionRecommendation, ...]:
    selected: list[DecisionRecommendation] = []
    for code, _prompt, choices in required_decisions:
        if code == "working_directory_override":
            continue
        configured = _SAFE_DEFAULTS.get(code)
        if configured is not None and configured[0] in choices:
            selected.append(DecisionRecommendation(code, configured[0], configured[1]))
        elif len(choices) == 1:
            selected.append(
                DecisionRecommendation(code, choices[0], "only available bounded option")
            )
    return tuple(selected)
