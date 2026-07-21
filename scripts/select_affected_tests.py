#!/usr/bin/env python3
"""Select the pytest files affected by the current change set.

Consumes the checked-in capability-group manifest at ``tests/test-groups.toml``
to map changed source paths to test groups. Any source path that is not
mapped by exactly the manifest, or any of a small always-wide set of files
that affect verification/build itself, forces the selection to escalate to
the full suite: this tool never silently narrows a run it cannot justify.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import tomli as tomllib

DEFAULT_MANIFEST = Path("tests/test-groups.toml")
DEFAULT_TESTS_DIR = Path("tests")

# Changes to these paths affect verification/build/selection itself and can
# invalidate any group mapping, so they always force a full-suite run rather
# than trusting the (possibly stale) manifest to select narrowly.
ALWAYS_WIDE_GLOBS: tuple[str, ...] = (
    "pyproject.toml",
    "uv.lock",
    "Makefile",
    "config.repoforge.toml",
    "tests/test-groups.toml",
    "tests/conftest.py",
    "scripts/select_affected_tests.py",
    "scripts/run_test_shards.py",
    "scripts/verify-production.sh",
    ".github/workflows/**",
)


@dataclass(frozen=True, slots=True)
class Group:
    name: str
    description: str
    parallel: bool
    source_globs: tuple[str, ...]
    test_files: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Manifest:
    groups: tuple[Group, ...]
    safety_bundle: tuple[str, ...]

    def group_by_name(self, name: str) -> Group:
        for group in self.groups:
            if group.name == name:
                return group
        raise KeyError(name)


@dataclass(frozen=True, slots=True)
class Selection:
    selected_groups: tuple[str, ...]
    selected_files: tuple[str, ...]
    omitted_groups: tuple[str, ...]
    reasons: tuple[str, ...]
    escalated_to_wide: bool
    escalation_reason: str | None


def _glob_to_regex(glob: str) -> re.Pattern[str]:
    """Translate a POSIX-style glob (supporting ``**``) to a regex."""
    parts: list[str] = []
    i = 0
    while i < len(glob):
        if glob[i : i + 3] == "**/":
            parts.append("(?:.*/)?")
            i += 3
        elif glob[i : i + 2] == "**":
            parts.append(".*")
            i += 2
        elif glob[i] == "*":
            parts.append("[^/]*")
            i += 1
        elif glob[i] == "?":
            parts.append("[^/]")
            i += 1
        else:
            parts.append(re.escape(glob[i]))
            i += 1
    return re.compile("^" + "".join(parts) + "$")


def _matches_any(path: str, globs: tuple[str, ...]) -> bool:
    return any(_glob_to_regex(glob).match(path) for glob in globs)


def load_manifest(path: Path = DEFAULT_MANIFEST) -> Manifest:
    with path.open("rb") as handle:
        raw = tomllib.load(handle)
    groups = []
    for name, payload in raw.get("groups", {}).items():
        groups.append(
            Group(
                name=name,
                description=str(payload.get("description", "")),
                parallel=bool(payload.get("parallel", False)),
                source_globs=tuple(payload.get("source_globs", [])),
                test_files=tuple(payload.get("test_files", [])),
            )
        )
    safety_bundle = tuple(raw.get("safety_bundle", {}).get("test_files", []))
    return Manifest(groups=tuple(groups), safety_bundle=safety_bundle)


def check_completeness(manifest: Manifest, tests_dir: Path = DEFAULT_TESTS_DIR) -> list[str]:
    """Return human-readable violations; an empty list means the manifest is complete."""
    violations: list[str] = []

    on_disk = {
        f"tests/{path.name}" for path in tests_dir.glob("test_*.py") if path.name != "conftest.py"
    }

    ownership: dict[str, list[str]] = {}
    for group in manifest.groups:
        for test_file in group.test_files:
            ownership.setdefault(test_file, []).append(group.name)

    for test_file, owners in sorted(ownership.items()):
        if len(owners) > 1:
            violations.append(
                f"test file {test_file!r} is claimed by more than one group: {sorted(owners)}"
            )
        if test_file not in on_disk:
            violations.append(f"manifest references {test_file!r}, which does not exist on disk")

    manifest_files = set(ownership)
    for test_file in sorted(on_disk - manifest_files):
        violations.append(f"test file {test_file!r} is not mapped to any group")

    for test_file in manifest.safety_bundle:
        if test_file not in manifest_files:
            violations.append(
                f"safety bundle references {test_file!r}, which is not owned by any group"
            )
        if test_file not in on_disk:
            violations.append(f"safety bundle references {test_file!r}, which does not exist")

    return violations


def select_affected_tests(manifest: Manifest, changed_paths: Sequence[str]) -> Selection:
    if not changed_paths:
        # Nothing changed: still run the safety bundle, select nothing else.
        return Selection(
            selected_groups=(),
            selected_files=tuple(sorted(set(manifest.safety_bundle))),
            omitted_groups=tuple(group.name for group in manifest.groups),
            reasons=("no changed paths",),
            escalated_to_wide=False,
            escalation_reason=None,
        )

    for path in changed_paths:
        if _matches_any(path, ALWAYS_WIDE_GLOBS):
            all_files = sorted(
                {test_file for group in manifest.groups for test_file in group.test_files}
                | set(manifest.safety_bundle)
            )
            return Selection(
                selected_groups=tuple(group.name for group in manifest.groups),
                selected_files=tuple(all_files),
                omitted_groups=(),
                reasons=(f"{path!r} matches an always-wide path",),
                escalated_to_wide=True,
                escalation_reason=f"changed path {path!r} affects verification/build itself",
            )

    matched_group_names: set[str] = set()
    reasons: list[str] = []
    unmapped: list[str] = []
    for path in changed_paths:
        matched_any = False
        for group in manifest.groups:
            if _matches_any(path, group.source_globs):
                matched_any = True
                matched_group_names.add(group.name)
                reasons.append(f"{path!r} -> {group.name!r}")
        if not matched_any:
            unmapped.append(path)
    reasons.sort()

    if unmapped:
        all_files = sorted(
            {test_file for group in manifest.groups for test_file in group.test_files}
            | set(manifest.safety_bundle)
        )
        return Selection(
            selected_groups=tuple(group.name for group in manifest.groups),
            selected_files=tuple(all_files),
            omitted_groups=(),
            reasons=tuple(reasons),
            escalated_to_wide=True,
            escalation_reason=(
                "changed paths with no matching group (fail-closed): " + ", ".join(sorted(unmapped))
            ),
        )

    # Canonical (manifest) order, independent of the order changed_paths arrived in.
    selected_groups = [group.name for group in manifest.groups if group.name in matched_group_names]
    selected_files = set(manifest.safety_bundle)
    for name in selected_groups:
        selected_files.update(manifest.group_by_name(name).test_files)
    omitted = tuple(group.name for group in manifest.groups if group.name not in selected_groups)
    return Selection(
        selected_groups=tuple(selected_groups),
        selected_files=tuple(sorted(selected_files)),
        omitted_groups=omitted,
        reasons=tuple(reasons),
        escalated_to_wide=False,
        escalation_reason=None,
    )


def changed_paths_from_git(root: Path, base_ref: str) -> list[str]:
    committed = subprocess.run(
        ["git", "diff", "--name-only", f"{base_ref}...HEAD"],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    uncommitted = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=normal"],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    paths = list(committed)
    for line in uncommitted:
        # Porcelain status lines look like "XY path" or "XY old -> new" for renames.
        candidate = line[3:].strip()
        if " -> " in candidate:
            candidate = candidate.split(" -> ", 1)[1]
        if candidate:
            paths.append(candidate)
    return sorted({path for path in paths if path})


def _print_report(selection: Selection) -> None:
    if selection.escalated_to_wide:
        print(f"[select-affected-tests] escalated to full suite: {selection.escalation_reason}")
    else:
        print(
            f"[select-affected-tests] selected groups: {', '.join(selection.selected_groups) or '(none)'}"
        )
        print(
            f"[select-affected-tests] omitted groups: {', '.join(selection.omitted_groups) or '(none)'}"
        )
    for reason in selection.reasons:
        print(f"[select-affected-tests] reason: {reason}")
    print(f"[select-affected-tests] {len(selection.selected_files)} test files selected")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--tests-dir", type=Path, default=DEFAULT_TESTS_DIR)
    parser.add_argument("--check-completeness", action="store_true")
    parser.add_argument("--base", default="main", help="Base ref for git diff (default: main)")
    parser.add_argument(
        "--path",
        dest="paths",
        action="append",
        default=None,
        help="Explicit changed path (repeatable); bypasses git when given",
    )
    parser.add_argument(
        "--run",
        action="store_true",
        help="Execute pytest against the selection (or the full suite if escalated)",
    )
    args = parser.parse_args(argv)

    manifest = load_manifest(args.manifest)

    if args.check_completeness:
        violations = check_completeness(manifest, args.tests_dir)
        if violations:
            for violation in violations:
                print(
                    f"[select-affected-tests] COMPLETENESS VIOLATION: {violation}", file=sys.stderr
                )
            return 1
        print("[select-affected-tests] manifest is complete")
        return 0

    root = Path.cwd()
    changed = args.paths if args.paths is not None else changed_paths_from_git(root, args.base)
    selection = select_affected_tests(manifest, changed)
    _print_report(selection)

    if args.run:
        if selection.escalated_to_wide:
            command = [
                sys.executable,
                "-m",
                "pytest",
                "--cov=repoforge",
                "--cov-report=term-missing",
            ]
        else:
            command = [sys.executable, "-m", "pytest", "-q", *selection.selected_files]
        completed = subprocess.run(command, cwd=root, check=False)
        return completed.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
