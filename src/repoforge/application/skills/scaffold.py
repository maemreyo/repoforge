"""Zero-config scaffold (#205): `rf rules init` and the optional onboard step.

Idempotent and working-tree-only: it never overwrites an existing file and never touches Git
(no `git add`, no commit) -- the operator reviews and commits scaffolded files like any other
change.
"""

from __future__ import annotations

from pathlib import Path

_STARTER_RULES_YAML = (
    "# .repoforge/rules/example.yaml\n"
    "# Uncomment and edit to add a repo-specific rule; see docs for the full schema.\n"
    "# - id: application.no-adapter-imports\n"
    "#   validator: import_boundary\n"
    '#   paths: ["src/**/*.py"]\n'
    '#   forbid: ["adapters"]\n'
    "#   override_policy: never\n"
    "#   delivery: always\n"
)
_STARTER_SKILLS_YAML = (
    "# .repoforge/skills.yaml\n"
    "# Bind a discovered skill (see .agents/skills, .claude/skills) to a path scope, phase,\n"
    "# and delivery class.\n"
    "# - skill: my-skill\n"
    '#   paths: ["src/**"]\n'
    "#   delivery: on_entry\n"
)


def scaffold_repoforge_rules(repo_root: Path) -> tuple[str, ...]:
    """Create `.repoforge/rules/example.yaml` and `.repoforge/skills.yaml` if absent.

    Returns the repo-relative paths actually created this call; an empty tuple means
    everything already existed (a second call is always a no-op diff-wise).
    """

    created: list[str] = []
    rules_dir = repo_root / ".repoforge" / "rules"
    rules_dir.mkdir(parents=True, exist_ok=True)

    example = rules_dir / "example.yaml"
    if not example.exists():
        example.write_text(_STARTER_RULES_YAML, encoding="utf-8")
        created.append(str(example.relative_to(repo_root)))

    skills_yaml = repo_root / ".repoforge" / "skills.yaml"
    if not skills_yaml.exists():
        skills_yaml.write_text(_STARTER_SKILLS_YAML, encoding="utf-8")
        created.append(str(skills_yaml.relative_to(repo_root)))

    return tuple(created)
