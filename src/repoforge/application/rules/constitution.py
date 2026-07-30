"""Compile the constitution from the accepted default-branch generation, never the worktree.

The rules, skill bindings and skill definitions that govern a task are *not* read from the
workspace the task is editing. A task proposing a change to `.repoforge/rules/` must be
judged by the accepted constitution, not by its own uncommitted edits -- otherwise a task
can widen the rules it is about to be checked against, in the same call. That is the
self-modification guard #206 and #209 both depend on, and it is why every read here goes
through the git snapshot API at a commit rather than through the filesystem.

`constitution_sha` identifies exactly which constitution was compiled. It is a hash over
the git object ids of the contributing files, so it changes when any of them changes and
stays stable when nothing does -- without depending on file order, mtimes, or the temporary
directory the loaders are pointed at.
"""

from __future__ import annotations

import hashlib
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from ...config import RepositoryConfig
from ...domain.errors import ConfigError, ErrorCode
from ...domain.rules_engine import Rule
from ...domain.skills import SkillCatalog
from ...ports.git import GitRepository
from ..skills.binding import SkillBinding, load_bindings
from ..skills.discovery import _REPO_ROOTS, discover_skills
from .loader import load_rules

CONSTITUTION_SCHEMA_VERSION = 1

#: Everything the constitution is compiled from. Rules and bindings live under
#: `.repoforge/`; skill definitions do not -- they live in the roots #205 discovers, so
#: those are derived from its own constant rather than restated, which would let the two
#: drift apart silently.
CONSTITUTION_PREFIXES: tuple[str, ...] = (
    ".repoforge/",
    *(f"{relative}/" for relative, _kind in _REPO_ROOTS),
)

_MAX_SNAPSHOT_ENTRIES = 20_000
_MAX_CONSTITUTION_FILES = 500
_MAX_CONSTITUTION_BYTES = 4_000_000
_SYMLINK_MODE = "120000"


def _error(message: str) -> ConfigError:
    return ConfigError(message, code=ErrorCode.CONFIG_INVALID)


@dataclass(frozen=True, slots=True)
class ConstitutionSource:
    """Which constitution was compiled, and from where."""

    resolved_ref: str
    commit_sha: str
    constitution_sha: str
    paths: tuple[str, ...]
    snapshot_truncated: bool


@dataclass(frozen=True, slots=True)
class CompiledConstitution:
    source: ConstitutionSource
    rules: tuple[Rule, ...]
    bindings: tuple[SkillBinding, ...]
    catalog: SkillCatalog


def _is_constitution_path(relative_path: str) -> bool:
    return any(relative_path.startswith(prefix) for prefix in CONSTITUTION_PREFIXES)


def _safe_relative(relative_path: str) -> PurePosixPath:
    """Refuse anything that would not land strictly inside the materialization root.

    Snapshot listings come from git, so these shapes are not expected -- which is exactly
    why they are refused rather than normalized away.
    """

    candidate = PurePosixPath(relative_path)
    if candidate.is_absolute() or ".." in candidate.parts or not candidate.parts:
        raise _error(f"Constitution path is not a safe relative path: {relative_path!r}")
    return candidate


def _constitution_digest(entries: tuple[tuple[str, str], ...]) -> str:
    """Hash (path, git object id) pairs, so content identity comes from git itself."""

    digest = hashlib.sha256()
    digest.update(f"constitution-v{CONSTITUTION_SCHEMA_VERSION}\0".encode())
    for prefix in CONSTITUTION_PREFIXES:
        digest.update(f"{prefix}\0".encode())
    for relative_path, object_sha in entries:
        digest.update(relative_path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(object_sha.encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def compile_constitution(
    *,
    git: GitRepository,
    path: Path,
    repo: RepositoryConfig,
    ref: str | None = None,
) -> CompiledConstitution:
    """Compile rules, bindings and the skill catalog from an accepted commit.

    ``ref`` defaults to the repository's accepted default branch. ``path`` is only used to
    address the git object store -- no file under it is read, which is what makes the
    guard hold for a workspace that has edited its own `.repoforge/`.
    """

    resolved = git.resolve_snapshot_ref(path, repo, ref)
    listed, snapshot_truncated = git.list_snapshot_files(
        path,
        repo,
        resolved.commit_sha,
        _MAX_SNAPSHOT_ENTRIES,
    )
    candidates = sorted(item for item in listed if _is_constitution_path(item))
    if len(candidates) > _MAX_CONSTITUTION_FILES:
        raise _error(
            f"Constitution at {resolved.commit_sha} spans more than {_MAX_CONSTITUTION_FILES} files"
        )

    entries: list[tuple[str, str]] = []
    total_bytes = 0
    with tempfile.TemporaryDirectory(prefix="repoforge-constitution-") as temporary:
        root = Path(temporary)
        for relative_path in candidates:
            blob = git.read_snapshot_blob(path, repo, resolved.commit_sha, relative_path)
            if blob.mode == _SYMLINK_MODE:
                # A symlink materialized into the tree would let the loaders read outside
                # it. The constitution has no legitimate use for one.
                raise _error(f"Constitution entry is a symlink: {relative_path!r}")
            total_bytes += blob.size_bytes
            if total_bytes > _MAX_CONSTITUTION_BYTES:
                raise _error(
                    f"Constitution at {resolved.commit_sha} exceeds {_MAX_CONSTITUTION_BYTES} bytes"
                )
            target = root / _safe_relative(relative_path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(blob.data)
            entries.append((relative_path, blob.object_sha))

        # The loaders landed under #204/#205 read a working tree, so they are pointed at
        # the materialized commit instead of being reworked to take a reader.
        rules = load_rules(root)
        bindings = load_bindings(root)
        catalog = discover_skills(root)

    return CompiledConstitution(
        source=ConstitutionSource(
            resolved_ref=resolved.resolved_ref,
            commit_sha=resolved.commit_sha,
            constitution_sha=_constitution_digest(tuple(entries)),
            paths=tuple(relative_path for relative_path, _sha in entries),
            snapshot_truncated=snapshot_truncated,
        ),
        rules=rules,
        bindings=bindings,
        catalog=catalog,
    )


__all__ = [
    "CONSTITUTION_PREFIXES",
    "CONSTITUTION_SCHEMA_VERSION",
    "CompiledConstitution",
    "ConstitutionSource",
    "compile_constitution",
]
