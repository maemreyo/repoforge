"""Built-in rule validators (#204): file_length, diff_size, import_boundary, new_dependency,
generated_no_hand_edit. Each reads only what the review context supplies -- no shell, no
network, no arbitrary command execution. A validator that lacks the context it needs to reach
a conclusion reports UNKNOWN, never guessing PASS or FAIL.
"""

from __future__ import annotations

import ast
import fnmatch
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

import tomli as tomllib

from ...domain.rules_engine import Finding, Rule, RuleResultState, sort_findings

_MAX_SCAN_FILES = 5_000
_MAX_FILE_BYTES = 2_000_000


@dataclass(frozen=True, slots=True)
class DiffStat:
    path: str
    added_lines: int
    removed_lines: int


@dataclass(frozen=True, slots=True)
class GeneratedPathSpec:
    glob: str
    regeneration_command: str
    description: str = ""


@dataclass(frozen=True, slots=True)
class ReviewContext:
    root: Path
    changed_paths: tuple[str, ...] = ()
    diff_stats: tuple[DiffStat, ...] = ()
    baseline_manifests: Mapping[str, str] = field(default_factory=dict)
    generated_paths: tuple[GeneratedPathSpec, ...] = ()
    regenerated_paths: frozenset[str] = frozenset()


def _matches(path: str, patterns: tuple[str, ...]) -> bool:
    for pattern in patterns:
        normalized = pattern.replace("\\", "/").removeprefix("./")
        candidates = [normalized]
        if normalized.startswith("**/"):
            candidates.append(normalized[3:])
        if any(
            fnmatch.fnmatchcase(path, item) or PurePosixPath(path).match(item)
            for item in candidates
        ):
            return True
    return False


def _iter_matching_files(context: ReviewContext, patterns: tuple[str, ...]) -> list[str]:
    matched: list[str] = []
    for index, candidate in enumerate(sorted(context.root.rglob("*"))):
        if index > _MAX_SCAN_FILES:
            break
        if not candidate.is_file():
            continue
        relative = candidate.relative_to(context.root).as_posix()
        if _matches(relative, patterns):
            matched.append(relative)
    return matched


def _read_text(root: Path, relative: str) -> str | None:
    path = root / relative
    try:
        if path.stat().st_size > _MAX_FILE_BYTES:
            return None
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def validate_file_length(rule: Rule, context: ReviewContext) -> list[Finding]:
    max_lines = rule.params.get("max_lines", 400)
    if not isinstance(max_lines, int) or max_lines <= 0:
        return [
            Finding(
                rule.id, "<rule>", 0, "max_lines must be a positive integer", RuleResultState.ERROR
            )
        ]
    findings: list[Finding] = []
    for relative in _iter_matching_files(context, rule.paths):
        text = _read_text(context.root, relative)
        if text is None:
            findings.append(
                Finding(
                    rule.id,
                    relative,
                    0,
                    "file could not be read for line counting",
                    RuleResultState.UNKNOWN,
                )
            )
            continue
        line_count = text.count("\n") + (1 if text and not text.endswith("\n") else 0)
        if line_count > max_lines:
            findings.append(
                Finding(
                    rule.id,
                    relative,
                    line_count,
                    f"file has {line_count} lines, exceeding the {max_lines}-line limit",
                    RuleResultState.FAIL,
                    fix_hint="Split this file into smaller, single-purpose modules.",
                )
            )
    return findings


def validate_diff_size(rule: Rule, context: ReviewContext) -> list[Finding]:
    if not context.diff_stats:
        return [
            Finding(
                rule.id,
                "<diff>",
                0,
                "no diff context was supplied for this run",
                RuleResultState.UNKNOWN,
                fix_hint="Run this rule with diff_stats bound to the changed tree.",
            )
        ]
    max_lines = rule.params.get("max_lines", 600)
    max_files = rule.params.get("max_files", 40)
    matching = [stat for stat in context.diff_stats if _matches(stat.path, rule.paths)]
    if not matching:
        return []
    total_lines = sum(stat.added_lines + stat.removed_lines for stat in matching)
    findings: list[Finding] = []
    if total_lines > max_lines:
        findings.append(
            Finding(
                rule.id,
                matching[0].path,
                total_lines,
                f"diff totals {total_lines} changed lines, exceeding the {max_lines}-line budget",
                RuleResultState.FAIL,
                fix_hint="Split this change into smaller, reviewable commits.",
            )
        )
    if len(matching) > max_files:
        findings.append(
            Finding(
                rule.id,
                matching[0].path,
                len(matching),
                f"diff touches {len(matching)} files, exceeding the {max_files}-file budget",
                RuleResultState.FAIL,
                fix_hint="Split this change into smaller, reviewable commits.",
            )
        )
    return findings


_DYNAMIC_IMPORT_CALLEES = {"import_module", "__import__"}


def _module_imports(text: str, relative: str) -> tuple[list[tuple[int, str]], list[int]]:
    """Return (line, dotted-target) for static imports, plus lines with an unresolved
    dynamic import call (`importlib.import_module(expr)` with a non-literal argument)."""

    try:
        tree = ast.parse(text, filename=relative)
    except SyntaxError:
        return [], []
    targets: list[tuple[int, str]] = []
    dynamic: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                targets.append((node.lineno, alias.name))
        elif isinstance(node, ast.ImportFrom) and node.module:
            targets.append((node.lineno, node.module))
        elif isinstance(node, ast.Call):
            name = (
                node.func.attr
                if isinstance(node.func, ast.Attribute)
                else (node.func.id if isinstance(node.func, ast.Name) else None)
            )
            if name in _DYNAMIC_IMPORT_CALLEES and node.args:
                first = node.args[0]
                if not (isinstance(first, ast.Constant) and isinstance(first.value, str)):
                    dynamic.append(node.lineno)
    return targets, dynamic


def validate_import_boundary(rule: Rule, context: ReviewContext) -> list[Finding]:
    forbidden = rule.params.get("forbid", [])
    if not isinstance(forbidden, list) or not all(isinstance(item, str) for item in forbidden):
        return [
            Finding(
                rule.id,
                "<rule>",
                0,
                "forbid must be a list of import-name prefixes",
                RuleResultState.ERROR,
            )
        ]
    findings: list[Finding] = []
    for relative in _iter_matching_files(context, rule.paths):
        if not relative.endswith(".py"):
            continue
        text = _read_text(context.root, relative)
        if text is None:
            findings.append(
                Finding(rule.id, relative, 0, "file could not be read", RuleResultState.UNKNOWN)
            )
            continue
        targets, dynamic_lines = _module_imports(text, relative)
        for line, target in targets:
            if any(target == name or target.startswith(name + ".") for name in forbidden):
                findings.append(
                    Finding(
                        rule.id,
                        relative,
                        line,
                        f"import of {target!r} is forbidden in this path",
                        RuleResultState.FAIL,
                        fix_hint=f"Remove the dependency on {target!r} or move this code out of {rule.paths[0]!r}.",
                    )
                )
        for line in dynamic_lines:
            findings.append(
                Finding(
                    rule.id,
                    relative,
                    line,
                    "dynamic import target could not be statically resolved",
                    RuleResultState.UNKNOWN,
                    fix_hint="Use a literal import target so the boundary check can verify it.",
                )
            )
    return findings


_PEP508_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


def _pyproject_dependencies(text: str) -> set[str] | None:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return None
    project = data.get("project")
    if not isinstance(project, dict):
        return set()
    deps = project.get("dependencies")
    if not isinstance(deps, list):
        return set()
    names: set[str] = set()
    for entry in deps:
        if isinstance(entry, str):
            match = _PEP508_NAME.match(entry)
            if match:
                names.add(match.group(1).lower())
    return names


def _package_json_dependencies(text: str) -> set[str] | None:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return set()
    names: set[str] = set()
    for key in ("dependencies", "devDependencies"):
        section = data.get(key)
        if isinstance(section, dict):
            names.update(str(name).lower() for name in section)
    return names


_MANIFEST_EXTRACTORS: dict[str, Callable[[str], set[str] | None]] = {
    "pyproject.toml": _pyproject_dependencies,
    "package.json": _package_json_dependencies,
}


def validate_new_dependency(rule: Rule, context: ReviewContext) -> list[Finding]:
    manifests = rule.params.get("manifests", list(_MANIFEST_EXTRACTORS))
    if not isinstance(manifests, list):
        return [Finding(rule.id, "<rule>", 0, "manifests must be a list", RuleResultState.ERROR)]
    findings: list[Finding] = []
    for manifest in manifests:
        extractor = _MANIFEST_EXTRACTORS.get(str(manifest))
        if extractor is None:
            continue
        current_text = _read_text(context.root, str(manifest))
        if current_text is None:
            continue
        baseline_text = context.baseline_manifests.get(str(manifest))
        if baseline_text is None:
            findings.append(
                Finding(
                    rule.id,
                    str(manifest),
                    0,
                    "no baseline manifest was supplied for this run",
                    RuleResultState.UNKNOWN,
                    fix_hint="Run this rule with baseline_manifests bound to the pre-change tree.",
                )
            )
            continue
        current = extractor(current_text)
        baseline = extractor(baseline_text)
        if current is None or baseline is None:
            findings.append(
                Finding(
                    rule.id,
                    str(manifest),
                    0,
                    "manifest could not be parsed",
                    RuleResultState.UNKNOWN,
                )
            )
            continue
        for added in sorted(current - baseline):
            findings.append(
                Finding(
                    rule.id,
                    str(manifest),
                    0,
                    f"new dependency added: {added}",
                    RuleResultState.FAIL,
                    fix_hint="Confirm this dependency is necessary before merging.",
                )
            )
    return findings


def validate_generated_no_hand_edit(rule: Rule, context: ReviewContext) -> list[Finding]:
    if not context.generated_paths:
        return [
            Finding(
                rule.id,
                "<rule>",
                0,
                "no generated_paths were declared for this repository",
                RuleResultState.SKIPPED,
            )
        ]
    globs = tuple(spec.glob for spec in context.generated_paths)
    findings: list[Finding] = []
    for changed in context.changed_paths:
        if not _matches(changed, globs) or changed in context.regenerated_paths:
            continue
        spec = next(
            (item for item in context.generated_paths if _matches(changed, (item.glob,))), None
        )
        command = (
            spec.regeneration_command if spec is not None else "the declared regeneration command"
        )
        findings.append(
            Finding(
                rule.id,
                changed,
                0,
                "generated file changed without a matching regeneration receipt",
                RuleResultState.FAIL,
                fix_hint=f"Regenerate this file with {command!r} instead of hand-editing it.",
            )
        )
    return findings


BUILTIN_VALIDATORS: dict[str, Callable[[Rule, ReviewContext], list[Finding]]] = {
    "file_length": validate_file_length,
    "diff_size": validate_diff_size,
    "import_boundary": validate_import_boundary,
    "new_dependency": validate_new_dependency,
    "generated_no_hand_edit": validate_generated_no_hand_edit,
}


@dataclass(frozen=True, slots=True)
class ReviewReport:
    findings: tuple[Finding, ...]

    def as_dict(self) -> dict[str, object]:
        return {"findings": [finding.as_dict() for finding in self.findings]}


def run_review(rules: tuple[Rule, ...], context: ReviewContext) -> ReviewReport:
    """Linter semantics: run every rule once and return ALL findings in one pass, never
    stopping at the first failure."""

    findings: list[Finding] = []
    for rule in rules:
        validator = BUILTIN_VALIDATORS.get(rule.validator)
        if validator is None:
            findings.append(
                Finding(
                    rule.id,
                    "<rule>",
                    0,
                    f"validator {rule.validator!r} is not registered",
                    RuleResultState.ERROR,
                )
            )
            continue
        findings.extend(validator(rule, context))
    return ReviewReport(tuple(sort_findings(findings)))
