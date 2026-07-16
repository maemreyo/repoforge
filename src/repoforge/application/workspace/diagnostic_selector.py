"""Resolve one or two typed diagnostic selectors into reviewed argv tokens.

Every selector value is validated individually against its declared, config-time
rules before it is placed into an argv element -- never through a shell, never via
user-supplied regex. Multi-value selectors expand either by repeating the
placeholder (one argv element per value) or by joining validated values with a
template-declared, allowlisted separator into a single argv element.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from ...config import RepositoryConfig
from ...domain.diagnostics import (
    MAX_ARGV_ELEMENTS,
    DiagnosticProfileConfig,
    DiagnosticSelectorConfig,
    DiagnosticSelectorKind,
    token_char_classes,
)
from ...domain.errors import ErrorCode, RepoForgeError, SecurityError
from ...domain.policy import assert_path_allowed, normalize_relative_path, resolve_workspace_path
from ...ports.git import GitRepository

_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@+:/-]{0,127}$")
_SHELL_META = frozenset(";&|`$<>")
_MAX_JOINED_LENGTH = 512
_PLACEHOLDER = re.compile(r"^\{selector(?::(?P<name>[a-z][a-z0-9_]{0,31}))?\}$")

SelectorInput = str | list[str] | None


def _placeholder_name(argument: str) -> str | None:
    """Return the selector name an argv element addresses, or None if it is literal."""
    match = _PLACEHOLDER.fullmatch(argument)
    if match is None:
        return None
    return match.group("name") or "selector"


@dataclass(frozen=True, slots=True)
class ResolvedDiagnosticSelector:
    value: str | None
    values: dict[str, tuple[str, ...]]
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


def _as_list(raw: SelectorInput) -> list[str] | None:
    if raw is None:
        return None
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, list) and all(isinstance(item, str) for item in raw):
        return list(raw)
    raise _error("Diagnostic selector must be a string or a list of strings")


def _basic_value(value: str, *, allow_leading_dash: bool) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 256
        or any(ord(character) < 32 for character in value)
        or any(character in _SHELL_META for character in value)
    ):
        raise _error("Diagnostic selector is empty, unsafe, or exceeds 256 characters")
    if value.startswith("-") and not allow_leading_dash:
        raise _error("Diagnostic selector cannot start with '-' for this template")
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


def _token_value(value: str, selector: DiagnosticSelectorConfig) -> str:
    if len(value) > selector.max_length:
        raise _error(
            f"Diagnostic selector '{selector.name}' exceeds its configured max_length of "
            f"{selector.max_length}"
        )
    classes = token_char_classes()
    allowed = frozenset().union(*(classes[name] for name in selector.char_classes))
    if any(character not in allowed for character in value):
        raise _error(
            f"Diagnostic selector '{selector.name}' contains a character outside its allowlisted classes"
        )
    if selector.prefix is not None and not value.startswith(selector.prefix):
        raise _error(f"Diagnostic selector '{selector.name}' must start with {selector.prefix!r}")
    if selector.suffix is not None and not value.endswith(selector.suffix):
        raise _error(f"Diagnostic selector '{selector.name}' must end with {selector.suffix!r}")
    return value


def _resolve_single(
    selector: DiagnosticSelectorConfig,
    raw_value: str,
    *,
    workspace: Path,
    repo: RepositoryConfig,
    git: GitRepository,
) -> str:
    value = _basic_value(raw_value, allow_leading_dash=selector.allow_leading_dash)
    kind = selector.kind
    if kind is DiagnosticSelectorKind.TRACKED_PATH:
        return _tracked_path(value, workspace=workspace, repo=repo, git=git)
    if kind is DiagnosticSelectorKind.PYTEST_NODE:
        path, separator, node = value.partition("::")
        normalized = _tracked_path(path, workspace=workspace, repo=repo, git=git)
        if separator and (not node or ".." in node or any(ch in _SHELL_META for ch in node)):
            raise _error("Pytest node selector is invalid")
        return normalized + (f"::{node}" if separator else "")
    if kind is DiagnosticSelectorKind.ENUM:
        if value not in selector.values:
            raise _error("Diagnostic selector is not in the reviewed enum allowlist")
        return value
    if kind in {DiagnosticSelectorKind.PACKAGE_NAME, DiagnosticSelectorKind.CHECK_ID}:
        if _SAFE_IDENTIFIER.fullmatch(value) is None or ".." in value:
            raise _error("Diagnostic identifier selector has an invalid format")
        return value
    if kind is DiagnosticSelectorKind.TOKEN:
        return _token_value(value, selector)
    raise _error(f"Unsupported diagnostic selector kind: {kind.value}")


def _resolve_values(
    selector: DiagnosticSelectorConfig,
    raw: SelectorInput,
    *,
    workspace: Path,
    repo: RepositoryConfig,
    git: GitRepository,
) -> tuple[str, ...]:
    if selector.kind is DiagnosticSelectorKind.NONE:
        if raw is not None:
            raise _error("This diagnostic does not accept a selector")
        return ()
    values = _as_list(raw)
    if values is None or not values:
        raise _error(
            f"Diagnostic selector '{selector.name}' requires at least one value", required=True
        )
    if len(values) > selector.max_values:
        raise _error(
            f"Diagnostic selector '{selector.name}' accepts at most {selector.max_values} value(s), "
            f"got {len(values)}"
        )
    return tuple(
        _resolve_single(selector, value, workspace=workspace, repo=repo, git=git)
        for value in values
    )


def _expand(selector: DiagnosticSelectorConfig, values: tuple[str, ...]) -> tuple[str, ...]:
    """Expand one selector's validated values into the argv element(s) it fills."""
    if not values:
        return ()
    if selector.expansion == "join":
        assert selector.separator is not None
        joined = selector.separator.join(values)
        if len(joined) > _MAX_JOINED_LENGTH:
            raise _error(
                f"Diagnostic selector '{selector.name}' joined value exceeds the "
                f"{_MAX_JOINED_LENGTH}-character bound"
            )
        return (joined,)
    return values


def resolve_diagnostic_selector(
    profile: DiagnosticProfileConfig,
    selector: SelectorInput,
    selector2: SelectorInput = None,
    *,
    workspace: Path,
    repo: RepositoryConfig,
    git: GitRepository,
) -> ResolvedDiagnosticSelector:
    """Validate every declared selector and substitute it into its own argv element(s)."""
    named_input: dict[str, SelectorInput] = {}
    profile_selectors = profile.selectors
    if profile_selectors:
        named_input[profile_selectors[0].name] = selector
    if len(profile_selectors) > 1:
        named_input[profile_selectors[1].name] = selector2
    elif selector2 is not None:
        raise _error("This diagnostic does not accept a second selector")

    resolved_values: dict[str, tuple[str, ...]] = {}
    for declared in profile_selectors:
        resolved_values[declared.name] = _resolve_values(
            declared,
            named_input.get(declared.name),
            workspace=workspace,
            repo=repo,
            git=git,
        )

    by_name = {declared.name: declared for declared in profile_selectors}
    argv: list[str] = []
    filled: set[str] = set()
    for argument in profile.argv_template:
        name = _placeholder_name(argument)
        matched = by_name.get(name) if name is not None else None
        if matched is None or matched.kind is DiagnosticSelectorKind.NONE:
            argv.append(argument)
            continue
        expanded = _expand(matched, resolved_values[matched.name])
        if not expanded:
            raise _error(f"Diagnostic selector '{matched.name}' produced no value")
        argv.extend(expanded)
        filled.add(matched.name)

    expected_names = {
        s.name for s in profile_selectors if s.kind is not DiagnosticSelectorKind.NONE
    }
    if filled != expected_names or any("{" in arg or "}" in arg for arg in argv):
        raise _error("Diagnostic argv template has unresolved or duplicate placeholders")
    if len(argv) > MAX_ARGV_ELEMENTS:
        raise _error(f"Diagnostic argv expanded beyond the {MAX_ARGV_ELEMENTS}-element bound")

    primary_values = resolved_values.get(profile.selector.name, ())
    primary_display = primary_values[0] if len(primary_values) == 1 else None
    return ResolvedDiagnosticSelector(primary_display, resolved_values, tuple(argv))
