"""Pure path, branch, and patch policy decisions."""

from __future__ import annotations

import fnmatch
import re
import shlex
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from ..config import RepositoryConfig
from .circuit_breakers import CircuitBreakerCategory, circuit_breaker_blocked
from .errors import SecurityError, WorkspaceError
from .workspace import WorkspaceKind

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
    validate_adopted_branch(branch, repo)


def validate_adopted_branch(branch: str, repo: RepositoryConfig) -> None:
    """Validate a branch the operator named themselves, WITHOUT the ``ai/`` prefix rule.

    Adopting an existing branch is an explicit instruction ("work on this branch"), so the
    naming convention is not enforced -- that convention exists to keep agent-created
    branches recognizable, and a branch the operator already made is theirs to name.

    Two checks are NOT relaxed, because neither is a convention:

    * a protected branch stays unwritable. Committing straight onto ``main`` is the one
      outcome a warning cannot undo, and it is never what "work on my branch" means.
    * the name must still be shell/ref safe. ``..``, a leading ``-`` or ``//`` are argument
      injection into git, not a style preference.
    """
    if branch in repo.protected_branches:
        raise circuit_breaker_blocked(
            CircuitBreakerCategory.PROTECTED_REF_WRITE,
            f"Protected branch is not writable: {branch}",
            safe_next_action=(
                "Choose a non-protected branch, or ask the operator to perform this write "
                "directly if the protected ref genuinely needs to change."
            ),
        )
    if (
        not _SAFE_BRANCH_RE.fullmatch(branch)
        or ".." in branch
        or branch.startswith("-")
        or branch.endswith("/")
        or ("//" in branch)
    ):
        raise SecurityError(f"Unsafe branch name: {branch!r}")


def validate_workspace_branch(kind: WorkspaceKind, branch: str, repo: RepositoryConfig) -> None:
    """Validate a branch for whichever kind of workspace it belongs to (#375).

    The single dispatch every call site must use instead of choosing between
    ``validate_branch``/``validate_adopted_branch`` itself: only ``managed_worktree``
    enforces the ai/* naming convention (#371's ``naming_convention_enforced``).
    ``adopted_worktree`` and ``attached_shared`` both validate as operator-named branches --
    the protected-branch and unsafe-name checks stay in every case; only the convention
    does not apply to a branch RepoForge did not name.
    """
    if kind.naming_convention_enforced:
        validate_branch(branch, repo)
    else:
        validate_adopted_branch(branch, repo)


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


def resolve_trusted_checkout(repo: RepositoryConfig, alias: str) -> Path:
    """Look up an operator-registered external checkout alias (#373).

    Never accepts a path from the caller -- only an alias the operator already put in
    static config. This is a lookup only: it does not itself prove the path is safe right
    now. The caller must re-resolve the returned path fresh (following any symlink) and
    validate repository identity and workspace_root containment against that live
    resolution on every call -- a stored snapshot cannot see a substitution introduced
    since the configuration was loaded, but a live identity check catches whatever the
    path currently, actually points to regardless of how it got there.
    """
    registered = repo.trusted_external_checkouts.get(alias)
    if registered is None:
        raise WorkspaceError(
            f"ATTACH_ALIAS_NOT_REGISTERED: {alias!r} is not a registered trusted checkout "
            f"for {repo.repo_id!r}",
            safe_next_action=(
                "Ask the operator to register this checkout in "
                "trusted_external_checkouts, or use attach_branch if it is a worktree of "
                "the repository's own primary checkout."
            ),
            unchanged_state=("No branch, worktree, or file was created.",),
        )
    return registered


_SCP_LIKE_REMOTE = re.compile(r"^(?:[\w.-]+@)?([\w.-]+):(.+)$")


def canonical_remote_identity(url: str) -> str | None:
    """Reduce a git remote URL to a host+path identity, independent of ssh vs https
    transport, a trailing `.git`, or trailing slashes -- so `git@github.com:a/b.git` and
    `https://github.com/a/b` compare equal. Returns None for blank input; this is a
    best-effort local comparison (no DNS/GitHub-API resolution), used to catch a fork or
    unrelated clone that happens to share root history with the enrolled repository but
    points at a different remote (#373 review finding) -- distinct from and in addition
    to the GitHub-API-backed identity check publication already applies at push time.
    """
    stripped = url.strip()
    if not stripped:
        return None
    if "://" in stripped:
        parsed = urlsplit(stripped)
        host = parsed.hostname or ""
        path = parsed.path
    else:
        scp = _SCP_LIKE_REMOTE.match(stripped)
        if scp is None:
            return None
        host, path = scp.group(1), scp.group(2)
    path = path.strip("/")
    if path.endswith(".git"):
        path = path[: -len(".git")]
    return f"{host.lower()}/{path.lower()}"
