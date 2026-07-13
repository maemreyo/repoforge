"""Pure path, branch, and patch policy decisions."""

from __future__ import annotations

import fnmatch
import re
import shlex
from pathlib import Path, PurePosixPath

from ..config import RepositoryConfig
from .errors import SecurityError

_SLUG_RE = re.compile("[^a-z0-9]+")
_SAFE_BRANCH_RE = re.compile("^[A-Za-z0-9._/-]+$")


def slugify(value: str, *, max_length: int = 48) -> str:
    slug = _SLUG_RE.sub("-", value.lower()).strip("-")[:max_length].rstrip("-")
    if not slug:
        raise SecurityError("Task slug must contain at least one letter or digit")
    return slug


def validate_branch(branch: str, repo: RepositoryConfig) -> None:
    if not branch.startswith(repo.branch_prefix):
        raise SecurityError(f"Branch must start with {repo.branch_prefix!r}")
    if branch in repo.protected_branches:
        raise SecurityError(f"Protected branch is not writable: {branch}")
    if (
        not _SAFE_BRANCH_RE.fullmatch(branch)
        or ".." in branch
        or branch.startswith("-")
        or branch.endswith("/")
        or ("//" in branch)
    ):
        raise SecurityError(f"Unsafe branch name: {branch!r}")


def normalize_relative_path(value: str) -> str:
    if not value or any(ord(c) < 32 for c in value):
        raise SecurityError("Path is empty or contains control characters")
    candidate = PurePosixPath(value.replace("\\", "/"))
    if candidate.is_absolute() or any(part in {"", ".", ".."} for part in candidate.parts):
        raise SecurityError(f"Path must be a normalized repository-relative path: {value!r}")
    return candidate.as_posix()


def _matches(path: str, pattern: str) -> bool:
    normalized = pattern.replace("\\", "/")
    if normalized.startswith("./"):
        normalized = normalized[2:]
    patterns = [normalized]
    if normalized.startswith("**/"):
        patterns.append(normalized[3:])
    return any(fnmatch.fnmatchcase(path, p) or PurePosixPath(path).match(p) for p in patterns)


def assert_path_allowed(path: str, repo: RepositoryConfig) -> str:
    normalized = normalize_relative_path(path)
    if repo.allowed_paths and (not any(_matches(normalized, p) for p in repo.allowed_paths)):
        raise SecurityError(f"Path is outside allowed_paths: {normalized}")
    if any(_matches(normalized, p) for p in repo.denied_paths):
        raise SecurityError(f"Path is denied by repository policy: {normalized}")
    return normalized


def resolve_workspace_path(
    workspace_root: Path, relative_path: str, repo: RepositoryConfig
) -> Path:
    normalized = assert_path_allowed(relative_path, repo)
    root = workspace_root.resolve(strict=True)
    candidate = (root / normalized).resolve(strict=False)
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise SecurityError(f"Path escapes workspace: {relative_path!r}") from exc
    return candidate


def extract_patch_paths(patch: str) -> tuple[str, ...]:
    paths = []
    for line in patch.splitlines():
        if line.startswith("diff --git "):
            try:
                parts = shlex.split(line)
            except ValueError as exc:
                raise SecurityError(f"Invalid diff header: {line!r}") from exc
            if len(parts) != 4:
                raise SecurityError(f"Invalid diff header: {line!r}")
            raws = parts[2:]
        elif line.startswith(("--- ", "+++ ")):
            raws = [line[4:].split("\t", 1)[0]]
        else:
            continue
        for raw in raws:
            if raw == "/dev/null":
                continue
            if raw.startswith(("a/", "b/")):
                raw = raw[2:]
            value = normalize_relative_path(raw)
            if value not in paths:
                paths.append(value)
    if not paths:
        raise SecurityError("Patch contains no file paths")
    return tuple(paths)


def validate_patch(patch: str, repo: RepositoryConfig, *, max_chars: int) -> tuple[str, ...]:
    if not patch.strip():
        raise SecurityError("Patch is empty")
    if len(patch) > max_chars:
        raise SecurityError(f"Patch exceeds maximum size of {max_chars} characters")
    mode = re.compile(
        "^(?:new file mode|deleted file mode|old mode|new mode) (?:120000|160000)$",
        re.MULTILINE,
    )
    index = re.compile("^index [0-9a-f]+\\.\\.[0-9a-f]+ (?:120000|160000)$", re.MULTILINE)
    if mode.search(patch) or index.search(patch):
        raise SecurityError("Patches that create or modify symlinks/submodules are not allowed")
    paths = extract_patch_paths(patch)
    for path in paths:
        assert_path_allowed(path, repo)
    return paths
