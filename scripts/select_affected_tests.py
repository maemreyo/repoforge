#!/usr/bin/env python3
"""Select the pytest files affected by the current change set.

Two selection strategies, in order of precision:

1. Coverage map (``tests/coverage-map.json``, built by ``build_coverage_map.py``
   / ``make test-map``): maps each ``src/repoforge/**.py`` module to the exact
   test files that execute its function bodies. A changed module selects only
   those tests. This is the default when the map is present.
2. Capability groups (``tests/test-groups.toml``): coarser source-glob -> group
   mapping, used for non-package paths (docs, data) and as the whole-selector
   fallback when the coverage map is absent.

Both strategies fail closed: an always-wide path (build/verification config), a
package module missing from the coverage map (new/uncovered), or any path with
no mapping at all escalates to the full suite -- this tool never silently
narrows a run it cannot justify.

This selection is load-bearing in CI: production-gate.yml uses it for pull
requests, and runs the full suite with coverage on every push to a protected
branch. So a stale map can make a PR run less precise, while the push-to-`main`
run is what still catches a cross-module regression before it reaches a release.
Regenerate with ``make test-map`` after material source/test changes.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import tomli as tomllib

DEFAULT_MANIFEST = Path("tests/test-groups.toml")
DEFAULT_TESTS_DIR = Path("tests")
DEFAULT_COVERAGE_MAP = Path("tests/coverage-map.json")

# Source paths that are Python modules under the package: a change here should
# be selectable through the coverage map. One that is NOT in the map is a new or
# uncovered module whose blast radius is unknown -> fail closed to the full suite.
_PACKAGE_SRC_PREFIX = "src/repoforge/"

# Changes to these paths affect verification/build/selection itself and can
# invalidate any group mapping, so they always force a full-suite run rather
# than trusting the (possibly stale) manifest to select narrowly.
#
# tests/conftest.py is deliberately NOT here: its blast radius is precisely
# the checked-in `conftest_consumers` list (see CONFTEST_PATH below), not the
# whole suite, because everything conftest.py exports feeds into one thing
# (the forge_env git fixture). --check-completeness re-derives that list from
# source and fails closed if a test starts using conftest.py without being
# added to the manifest.
ALWAYS_WIDE_GLOBS: tuple[str, ...] = (
    "pyproject.toml",
    "uv.lock",
    "Makefile",
    "tests/test-groups.toml",
    "scripts/select_affected_tests.py",
    "scripts/run_test_suite.py",
    "scripts/verify-production.sh",
    ".github/workflows/**",
)

CONFTEST_PATH = "tests/conftest.py"

# Coverage contexts cannot express every capability-preflight dependency: the
# protocol has no executable body, and composition/binding tests exercise a
# preflight consumer through a wider workflow. Keep those reviewed direct
# consumers beside the ordinary coverage map so source changes remain narrow
# without weakening the default fail-closed behavior for unknown modules.
_GITHUB_CAPABILITY_PREFLIGHT_CONSUMERS: dict[str, tuple[str, ...]] = {
    "src/repoforge/domain/github_capability_preflight.py": (
        "tests/test_github_capability_preflight_domain.py",
    ),
    "src/repoforge/ports/github_capability_preflight.py": (
        "tests/test_github_capability_preflight_adapter.py",
    ),
    "src/repoforge/adapters/github/capability_preflight.py": (
        "tests/test_github_capability_preflight_adapter.py",
    ),
    "src/repoforge/adapters/github/api_identity.py": (
        "tests/test_github_api_identity.py",
        "tests/test_github_capability_preflight_adapter.py",
    ),
    "src/repoforge/domain/repository_identity.py": (
        "tests/test_github_api_identity.py",
        "tests/test_operation_identity_leases.py",
        "tests/test_repository_identity_contracts.py",
    ),
    "src/repoforge/adapters/publication.py": (
        "tests/test_github_api_identity.py",
        "tests/test_publication_adapter.py",
    ),
    "src/repoforge/application/publication.py": (
        "tests/test_github_api_identity.py",
        "tests/test_publication_guards.py",
    ),
    "src/repoforge/application/context.py": (
        "tests/test_github_api_identity.py",
        "tests/test_integration.py",
    ),
    "src/repoforge/bootstrap.py": (
        "tests/test_github_api_identity.py",
        "tests/test_integration.py",
    ),
}

# Any export from tests/conftest.py that a test file might reference. Kept in
# sync with the checked-in `conftest_consumers` list by --check-completeness.
_CONFTEST_SYMBOL_RE = re.compile(
    r"\b(forge_env|create_forge_environment|ForgeEnvironment|execution_coordinator_for_tests|build_test_service)\b"
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
    conftest_consumers: tuple[str, ...]
    # src/repoforge/<file>.py -> test files that execute it (from coverage).
    # Empty when tests/coverage-map.json is absent; selection then falls back
    # to group source_globs.
    coverage_map: dict[str, tuple[str, ...]] = field(default_factory=dict)

    def group_by_name(self, name: str) -> Group:
        for group in self.groups:
            if group.name == name:
                return group
        raise KeyError(name)

    def serial_files(self) -> frozenset[str]:
        """Test files owned by a `parallel = false` group.

        These carry a known worker-contention risk under xdist (see the
        `run_test_suite.py` serial-lane comment) and must run outside any
        `-n` invocation, never mixed into the same pytest process as the
        parallel lane.
        """
        return frozenset(
            test_file
            for group in self.groups
            if not group.parallel
            for test_file in group.test_files
        )


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
    conftest_consumers = tuple(raw.get("conftest_consumers", {}).get("test_files", []))
    coverage_map = _load_coverage_map(path.parent / DEFAULT_COVERAGE_MAP.name)
    return Manifest(
        groups=tuple(groups),
        safety_bundle=safety_bundle,
        conftest_consumers=conftest_consumers,
        coverage_map=coverage_map,
    )


def _load_coverage_map(path: Path) -> dict[str, tuple[str, ...]]:
    """Load the source->test coverage map, or {} when it is absent/invalid.

    Absent map degrades gracefully to group-based selection, so the selector
    keeps working before the first `make test-map` and if the file is corrupt.
    """
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {
        str(src): tuple(str(t) for t in tests)
        for src, tests in raw.items()
        if isinstance(tests, list)
    }


def _actual_conftest_consumers(tests_dir: Path) -> set[str]:
    """Re-derive, from source, every test file that references a conftest.py export."""
    consumers: set[str] = set()
    for path in tests_dir.glob("test_*.py"):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if _CONFTEST_SYMBOL_RE.search(text):
            consumers.add(f"tests/{path.name}")
    return consumers


def is_mappable_module(source: str, path: str = "") -> bool:
    """Can coverage ever attribute a test to this module?

    Only executed *function-body* lines carry a per-test context, so a module whose
    functions are all `...`/`pass`/docstring -- every `ports/` Protocol -- and one with no
    functions at all -- `__init__.py`, generated constants -- can never appear in the map
    no matter how many tests import it.

    This distinction is the difference between a useful gate and a broken one. Without it
    the check told the author to run `make test-map`, which cannot fix those files: on the
    identity branch 15 of the 16 it rejected were Protocols and `__init__.py`, and no
    amount of regeneration would have satisfied it.
    """
    # A package `__init__.py` is a re-export surface. Where it does carry code it is a lazy
    # `__getattr__` production never reaches, because modules import submodules directly.
    # Not a convention call: zero of the 343 mapped files are `__init__.py`.
    if path.endswith("__init__.py"):
        return False
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False
    functions = [
        node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    if not functions:
        return False
    return any(
        not (
            isinstance(statement, ast.Pass)
            or (isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Constant))
        )
        for function in functions
        for statement in function.body
    )


def check_map_freshness(manifest: Manifest, changed_paths: Sequence[str], root: Path) -> int:
    """Fail when a changed package module could be in the coverage map but is not.

    This is the pull-request half of the map's staleness gate. Rebuilding the map needs a
    full recording run, so the authoritative check is push-only -- but by then the stale
    entry is already on `main`. This one is static: it reads the diff and the map, runs no
    tests, and answers in under a second.

    An unmapped module is not merely untidy. The selector fails closed on it, so every
    later change touching that file runs the whole suite; 170 such files accumulated when
    nothing was watching, and five of the last eight commits ran everything as a result.

    What it must NOT do is demand the impossible: see `is_mappable_module`.
    """
    unmapped: list[str] = []
    unmappable: list[str] = []
    for path in sorted(set(changed_paths)):
        if not path.startswith(_PACKAGE_SRC_PREFIX) or not path.endswith(".py"):
            continue
        if path in manifest.coverage_map:
            continue
        source_path = root / path
        if not source_path.exists():
            continue  # deleted in this change; the map is corrected by the next rebuild
        try:
            source = source_path.read_text(encoding="utf-8")
        except OSError:
            continue
        (unmapped if is_mappable_module(source, path) else unmappable).append(path)

    if unmappable:
        print(
            f"[select-affected-tests] {len(unmappable)} changed module(s) cannot be mapped at "
            "all (no executed function bodies: Protocol, __init__, constants) -- not a "
            "staleness signal"
        )
    if not unmapped:
        print("[select-affected-tests] every mappable changed module is in the coverage map")
        return 0
    print(
        f"[select-affected-tests] {len(unmapped)} changed module(s) have executable bodies but "
        "are missing from the coverage map, so every later change to them runs the whole suite:",
        file=sys.stderr,
    )
    for path in unmapped:
        print(f"    {path}", file=sys.stderr)
    print(
        "[select-affected-tests] run `make test-map` and commit tests/coverage-map.json",
        file=sys.stderr,
    )
    return 1


def check_completeness(manifest: Manifest, tests_dir: Path = DEFAULT_TESTS_DIR) -> list[str]:
    """Return human-readable violations; an empty list means the manifest is complete."""
    violations: list[str] = []

    on_disk = {
        f"tests/{path.name}" for path in tests_dir.glob("test_*.py") if path.name != "conftest.py"
    }

    actual_conftest_consumers = _actual_conftest_consumers(tests_dir)
    manifest_conftest_consumers = set(manifest.conftest_consumers)
    for test_file in sorted(actual_conftest_consumers - manifest_conftest_consumers):
        violations.append(
            f"test file {test_file!r} references tests/conftest.py's forge_env machinery "
            "but is not listed under [conftest_consumers] in tests/test-groups.toml"
        )
    for test_file in sorted(manifest_conftest_consumers - actual_conftest_consumers):
        violations.append(
            f"[conftest_consumers] lists {test_file!r}, which no longer references "
            "tests/conftest.py's forge_env machinery (stale entry)"
        )
    for test_file in sorted(manifest_conftest_consumers - on_disk):
        violations.append(f"[conftest_consumers] references {test_file!r}, which does not exist")

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

    # Coverage map (when present) must not reference deleted/renamed test files:
    # a stale entry would silently select nothing for that source file.
    mapped_tests = {test_file for tests in manifest.coverage_map.values() for test_file in tests}
    for test_file in sorted(mapped_tests - on_disk):
        violations.append(
            f"coverage map references {test_file!r}, which does not exist "
            "(regenerate with `make test-map`)"
        )

    return violations


def _escalated_selection(
    manifest: Manifest, reasons: Sequence[str], escalation_reason: str
) -> Selection:
    """Full-suite fallback used whenever a change's blast radius is unknown."""
    all_files = sorted(
        {test_file for group in manifest.groups for test_file in group.test_files}
        | set(manifest.safety_bundle)
        | set(manifest.conftest_consumers)
    )
    return Selection(
        selected_groups=tuple(group.name for group in manifest.groups),
        selected_files=tuple(all_files),
        omitted_groups=(),
        reasons=tuple(reasons),
        escalated_to_wide=True,
        escalation_reason=escalation_reason,
    )


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

    # tests/conftest.py is handled separately from ALWAYS_WIDE_GLOBS: its blast
    # radius is the checked-in conftest_consumers list, not the full suite.
    conftest_changed = CONFTEST_PATH in changed_paths
    remaining_paths = [path for path in changed_paths if path != CONFTEST_PATH]

    for path in remaining_paths:
        if _matches_any(path, ALWAYS_WIDE_GLOBS):
            return _escalated_selection(
                manifest,
                reasons=(f"{path!r} matches an always-wide path",),
                escalation_reason=f"changed path {path!r} affects verification/build itself",
            )

    # Prefer the coverage map (precise, per-file blast radius) when it exists;
    # otherwise fall back to the coarser group source_globs mapping.
    if manifest.coverage_map:
        return _select_via_coverage(manifest, remaining_paths, conftest_changed)
    return _select_via_groups(manifest, remaining_paths, conftest_changed)


def _select_via_coverage(
    manifest: Manifest, remaining_paths: list[str], conftest_changed: bool
) -> Selection:
    """Select the exact test files that execute each changed source module.

    A changed test file runs itself. A package module (src/repoforge/**.py) not
    present in the coverage map is new or uncovered -- its blast radius is
    unknown, so we fail closed to the full suite. Non-package or non-.py paths
    (docs, data) fall back to the group source_globs.
    """
    selected_files: set[str] = set(manifest.safety_bundle)
    reasons: list[str] = []
    unmapped: list[str] = []

    for path in remaining_paths:
        if path.startswith("tests/") and path.endswith(".py"):
            selected_files.add(path)
            reasons.append(f"{path!r} -> itself (changed test)")
        elif path.startswith(_PACKAGE_SRC_PREFIX) and path.endswith(".py"):
            covering = manifest.coverage_map.get(path)
            direct_consumers = _GITHUB_CAPABILITY_PREFLIGHT_CONSUMERS.get(path, ())
            if covering is None and not direct_consumers:
                unmapped.append(path)
            else:
                selected_files.update(covering or ())
                selected_files.update(direct_consumers)
                reasons.append(
                    f"{path!r} -> {len(covering or ())} covering test file(s), "
                    f"{len(direct_consumers)} reviewed direct consumer(s)"
                )
        else:
            matched = False
            for group in manifest.groups:
                if _matches_any(path, group.source_globs):
                    matched = True
                    selected_files.update(group.test_files)
                    reasons.append(f"{path!r} -> {group.name!r}")
            if not matched:
                unmapped.append(path)

    if unmapped:
        return _escalated_selection(
            manifest,
            reasons=sorted(reasons),
            escalation_reason=(
                "changed paths with no coverage/group mapping (fail-closed): "
                + ", ".join(sorted(unmapped))
            ),
        )

    if conftest_changed:
        selected_files.update(manifest.conftest_consumers)
        reasons.append(f"{CONFTEST_PATH!r} -> 'conftest_consumers'")

    return Selection(
        selected_groups=(),
        selected_files=tuple(sorted(selected_files)),
        omitted_groups=(),
        reasons=tuple(sorted(reasons)),
        escalated_to_wide=False,
        escalation_reason=None,
    )


def _select_via_groups(
    manifest: Manifest, remaining_paths: list[str], conftest_changed: bool
) -> Selection:
    matched_group_names: set[str] = set()
    reasons: list[str] = []
    unmapped: list[str] = []
    for path in remaining_paths:
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
        return _escalated_selection(
            manifest,
            reasons=reasons,
            escalation_reason=(
                "changed paths with no matching group (fail-closed): " + ", ".join(sorted(unmapped))
            ),
        )

    # Canonical (manifest) order, independent of the order changed_paths arrived in.
    selected_groups = [group.name for group in manifest.groups if group.name in matched_group_names]
    selected_files = set(manifest.safety_bundle)
    for name in selected_groups:
        selected_files.update(manifest.group_by_name(name).test_files)

    if conftest_changed:
        selected_files.update(manifest.conftest_consumers)
        reasons = sorted([*reasons, f"{CONFTEST_PATH!r} -> 'conftest_consumers'"])
        selected_groups = [*selected_groups, "conftest_consumers"]

    omitted = tuple(group.name for group in manifest.groups if group.name not in selected_groups)
    return Selection(
        selected_groups=tuple(selected_groups),
        selected_files=tuple(sorted(selected_files)),
        omitted_groups=omitted,
        reasons=tuple(reasons),
        escalated_to_wide=False,
        escalation_reason=None,
    )


def _all_files_selection(manifest: Manifest) -> Selection:
    """Select every test file, as if every always-wide path had changed."""
    return _escalated_selection(
        manifest,
        reasons=("--full requested",),
        escalation_reason="--full requested: running every test file",
    )


def _run_in_lanes(root: Path, files: Sequence[str], manifest: Manifest) -> int:
    """Run `files` split into a serial lane and an xdist lane.

    Files owned by a `parallel = false` group carry a known worker-contention
    risk under xdist (see Group.serial_files) and must never share a pytest
    process with `-n`. They run first, alone; the rest run under `-n 3`.
    Mirrors the split `run_test_suite.py` already does for `make check`.
    """
    serial_files = manifest.serial_files()
    serial = sorted(f for f in files if f in serial_files)
    parallel = sorted(f for f in files if f not in serial_files)

    returncode = 0
    if serial:
        print(f"[select-affected-tests] serial lane: {len(serial)} test files")
        completed = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", *serial], cwd=root, check=False
        )
        returncode = returncode or completed.returncode
    if parallel:
        print(f"[select-affected-tests] xdist lane: {len(parallel)} test files")
        completed = subprocess.run(
            [sys.executable, "-m", "pytest", "-n", "3", "-q", *parallel], cwd=root, check=False
        )
        returncode = returncode or completed.returncode
    return returncode


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
    parser.add_argument(
        "--check-map-freshness",
        action="store_true",
        help=(
            "Fail when a changed package module is missing from the coverage map. "
            "Static: reads the diff and the map, runs no tests."
        ),
    )
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
    parser.add_argument(
        "--full",
        action="store_true",
        help="Select every test file, bypassing git diff (for fast full-suite runs)",
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

    if args.check_map_freshness:
        root = Path.cwd()
        changed = args.paths if args.paths is not None else changed_paths_from_git(root, args.base)
        return check_map_freshness(manifest, changed, root)

    root = Path.cwd()
    if args.full:
        selection = _all_files_selection(manifest)
    else:
        changed = args.paths if args.paths is not None else changed_paths_from_git(root, args.base)
        selection = select_affected_tests(manifest, changed)
    _print_report(selection)

    if args.run:
        # Coverage-gated authoritative runs belong to `make test`; this tool's
        # job is fast affected-test feedback, split into a serial lane (files
        # from `parallel = false` groups) and an xdist lane, same split
        # `run_test_suite.py` already uses for `make check`.
        return _run_in_lanes(root, selection.selected_files, manifest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
