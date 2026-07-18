"""Skill discovery (#205): reads SKILL.md directory packages as untrusted input.

Everything under a skill's `scripts/` directory is indexed as a bounded list of relative
filenames only -- never opened for content beyond what a caller explicitly reads as text via
:func:`read_skill_file`, and never passed to a command executor. That is the trust boundary:
ingestion may expose script content as text; it may never grant execution capability.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import yaml

from ...domain.skills import Skill, SkillCatalog, SkillRootKind, resolve_collisions

_MAX_SKILL_MD_BYTES = 200_000
_MAX_SKILLS_PER_ROOT = 500
_MAX_SCRIPT_FILES = 200

_REPO_ROOTS: tuple[tuple[str, SkillRootKind], ...] = (
    (".agents/skills", SkillRootKind.AGENTS),
    (".claude/skills", SkillRootKind.CLAUDE),
    (".agent/skills", SkillRootKind.AGENT_LEGACY),
)


def _sanitize(value: object, *, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    cleaned = "".join(ch for ch in value if ch == "\n" or ch == "\t" or ord(ch) >= 32)
    return cleaned.strip()[:limit]


def _parse_frontmatter(text: str) -> dict[str, object]:
    if not text.startswith("---"):
        return {}
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}
    try:
        data = yaml.safe_load(parts[1])
    except yaml.YAMLError:
        return {}
    return data if isinstance(data, dict) else {}


def _script_files(skill_dir: Path) -> tuple[str, ...]:
    scripts_dir = skill_dir / "scripts"
    if not scripts_dir.is_dir():
        return ()
    files: list[str] = []
    for index, candidate in enumerate(sorted(scripts_dir.rglob("*"))):
        if index >= _MAX_SCRIPT_FILES:
            break
        if candidate.is_file():
            files.append(candidate.relative_to(skill_dir).as_posix())
    return tuple(files)


def _load_one_skill(skill_dir: Path, *, kind: SkillRootKind, display_path: str) -> Skill | None:
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.is_file():
        return None
    try:
        raw = skill_md.read_bytes()[:_MAX_SKILL_MD_BYTES]
        text = raw.decode("utf-8", errors="replace")
    except OSError:
        return None
    frontmatter = _parse_frontmatter(text)
    name = _sanitize(frontmatter.get("name"), limit=128) or skill_dir.name
    description = _sanitize(frontmatter.get("description"), limit=2_000)
    upstream_raw = frontmatter.get("upstream")
    upstream = _sanitize(upstream_raw, limit=256) or None if isinstance(upstream_raw, str) else None
    digest = hashlib.sha256(raw).hexdigest()
    return Skill(
        name=name,
        description=description,
        root_kind=kind,
        path=display_path,
        digest=digest,
        scripts=_script_files(skill_dir),
        upstream=upstream,
    )


def _discover_one_root(root: Path, *, kind: SkillRootKind, display_prefix: str) -> list[Skill]:
    if not root.is_dir():
        return []
    found: list[Skill] = []
    for index, entry in enumerate(sorted(root.iterdir())):
        if index >= _MAX_SKILLS_PER_ROOT:
            break
        if not entry.is_dir():
            continue
        display_path = f"{display_prefix}/{entry.name}" if display_prefix else str(entry)
        skill = _load_one_skill(entry, kind=kind, display_path=display_path)
        if skill is not None:
            found.append(skill)
    return found


def discover_skills(repo_root: Path, *, user_roots: tuple[Path, ...] = ()) -> SkillCatalog:
    """Discover skills from the repo's default trio plus configured (read-only) user roots.

    Absent-root and empty-root cases both yield an empty-but-valid catalog -- discovery never
    requires `.repoforge/` to exist (zero-config).
    """

    skills: list[Skill] = []
    for relative, kind in _REPO_ROOTS:
        skills.extend(_discover_one_root(repo_root / relative, kind=kind, display_prefix=relative))
    for user_root in user_roots:
        skills.extend(_discover_one_root(user_root, kind=SkillRootKind.USER, display_prefix=""))
    return resolve_collisions(tuple(skills))


def read_skill_file(repo_root: Path, skill: Skill, relative_file: str) -> str | None:
    """Return one file's content from inside a skill directory as plain text -- the only
    sanctioned way to inspect `scripts/`/`references/`/`assets/` content. Never returns a
    path or handle suitable for execution."""

    base = Path(skill.path) if skill.root_kind is SkillRootKind.USER else repo_root / skill.path
    candidate = (base / relative_file).resolve()
    try:
        candidate.relative_to(base.resolve())
    except ValueError:
        return None  # path escape attempt
    if not candidate.is_file():
        return None
    try:
        return candidate.read_bytes()[:_MAX_SKILL_MD_BYTES].decode("utf-8", errors="replace")
    except OSError:
        return None
