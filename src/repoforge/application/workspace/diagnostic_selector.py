"""Resolve one typed diagnostic selector into one reviewed argv token."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from ...config import RepositoryConfig
from ...domain.diagnostics import DiagnosticProfileConfig, DiagnosticSelectorKind
from ...domain.errors import ErrorCode, RepoForgeError, SecurityError
from ...domain.policy import assert_path_allowed, normalize_relative_path, resolve_workspace_path
from ...ports.git import GitRepository

_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@+:/-]{0,127}$")
_SHELL_META = frozenset(";&|`$<>")


@dataclass(frozen=True, slots=True)
class ResolvedDiagnosticSelector:
    value: str | None
    argv: tuple[str, ...]


def _error(message: str, *, required: bool = False) -> RepoForgeError:
    return RepoForgeError(
        message,
        code=(
            ErrorCode.DIAGNOSTIC_SELECTOR_REQUIRED
            if required
            else ErrorCode.DIAGNOSTIC_SELECTOR_INVALID
        ),
        unchanged_state=("The workspace and diagnostic configuration were not modified.",),
        safe_next_action="Use a selector that matches the diagnostic's reviewed selector schema.",
    )


def _basic_selector(value: str | None, *, required: bool) -> str | None:
    if value is None:
        if required:
            raise _error("This diagnostic requires a selector", required=True)
        return None
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 256
        or value.startswith("-")
        or any(ord(character) < 32 for character in value)
        or any(character in _SHELL_META for character in value)
    ):
        raise _error("Diagnostic selector is empty, unsafe, or exceeds 256 characters")
    return value


def _tracked_path(
    raw_path: str,
    *,
    workspace: Path,
    repo: RepositoryConfig,
    git: GitRepository,
) -> str:
    try:
        normalized = assert_path_allowed(normalize_relative_path(raw_path), repo)
        candidate = resolve_workspace_path(workspace, normalized, repo)
    except SecurityError as exc:
        raise _error(str(exc)) from exc
    if not candidate.is_file() or candidate.is_symlink():
        raise _error(f"Diagnostic selector is not a tracked regular file: {normalized}")
    if not git.is_tracked_path(workspace, normalized):
        raise _error(f"Diagnostic selector is not tracked by Git: {normalized}")
    return normalized


def _resolve_value(
    profile: DiagnosticProfileConfig,
    selector: str | None,
    *,
    workspace: Path,
    repo: RepositoryConfig,
    git: GitRepository,
) -> str | None:
    kind = profile.selector.kind
    if kind is DiagnosticSelectorKind.NONE:
        if selector is not None:
            raise _error("This diagnostic does not accept a selector")
        return None

    value = _basic_selector(selector, required=True)
    assert value is not None
    if kind is DiagnosticSelectorKind.TRACKED_PATH:
        return _tracked_path(value, workspace=workspace, repo=repo, git=git)
    if kind is DiagnosticSelectorKind.PYTEST_NODE:
        path, separator, node = value.partition("::")
        normalized = _tracked_path(path, workspace=workspace, repo=repo, git=git)
        if separator and (not node or ".." in node or any(ch in _SHELL_META for ch in node)):
            raise _error("Pytest node selector is invalid")
        return normalized + (f"::{node}" if separator else "")
    if kind is DiagnosticSelectorKind.ENUM:
        if value not in profile.selector.values:
            raise _error("Diagnostic selector is not in the reviewed enum allowlist")
        return value
    if kind in {DiagnosticSelectorKind.PACKAGE_NAME, DiagnosticSelectorKind.CHECK_ID}:
        if _SAFE_IDENTIFIER.fullmatch(value) is None or ".." in value:
            raise _error("Diagnostic identifier selector has an invalid format")
        return value
    raise _error(f"Unsupported diagnostic selector kind: {kind.value}")


def resolve_diagnostic_selector(
    profile: DiagnosticProfileConfig,
    selector: str | None,
    *,
    workspace: Path,
    repo: RepositoryConfig,
    git: GitRepository,
) -> ResolvedDiagnosticSelector:
    """Validate one selector and substitute it into exactly one immutable argv element."""
    resolved = _resolve_value(
        profile,
        selector,
        workspace=workspace,
        repo=repo,
        git=git,
    )
    argv: list[str] = []
    replacements = 0
    for argument in profile.argv_template:
        if "{selector}" in argument:
            if argument != "{selector}" or resolved is None:
                raise _error(
                    "Diagnostic selector placeholder must occupy one complete argv element"
                )
            argv.append(resolved)
            replacements += 1
        else:
            argv.append(argument)
    expected = 0 if profile.selector.kind is DiagnosticSelectorKind.NONE else 1
    if replacements != expected or any("{" in argument or "}" in argument for argument in argv):
        raise _error("Diagnostic argv template has unresolved or duplicate placeholders")
    return ResolvedDiagnosticSelector(resolved, tuple(argv))
