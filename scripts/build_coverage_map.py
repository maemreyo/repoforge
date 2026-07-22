#!/usr/bin/env python3
"""Build the source-file -> covering-test-file map for coverage-based selection.

Runs the whole suite once under ``coverage``'s per-test context recording
(``--cov-context=test``), then inverts the result into a checked-in map:

    {"src/repoforge/<file>.py": ["tests/test_a.py", "tests/test_b.py"], ...}

``select_affected_tests.py`` consumes this map: a changed source file selects
exactly the test files that execute it, instead of a whole capability group.
Regenerate after material source/test changes (``make test-map``); CI can
regenerate and diff to catch drift.

The suite runs in the same two lanes as ``run_test_shards.py`` (serial-lane
groups alone, then the rest under ``-n 3``) so genuinely stateful tests do not
corrupt each other during the recording run. Test failures during generation
do not abort the map: coverage for everything that did run is still recorded,
and the selector fails closed on any source path the map does not cover.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import select_affected_tests as selector

DEFAULT_MAP_PATH = Path("tests/coverage-map.json")


def _run_pytest(
    root: Path, coverage_file: Path, files: list[str], *, xdist: bool, append: bool
) -> None:
    command = [
        sys.executable,
        "-m",
        "pytest",
        "-p",
        "no:cacheprovider",
        "-q",
        "--cov=repoforge",
        "--cov-context=test",
        "--cov-report=",
        "--cov-fail-under=0",
        "-o",
        "addopts=",  # drop the repo default --timeout etc.; recording run only
    ]
    if append:
        command.append("--cov-append")
    if xdist:
        command += ["-n", "3"]
    command += files
    env = dict(os.environ)
    env["COVERAGE_FILE"] = str(coverage_file)
    # check=False: a flaky/failing test must not abort map generation.
    subprocess.run(command, cwd=root, env=env, check=False)


def _record_coverage(root: Path, coverage_file: Path) -> None:
    manifest = selector.load_manifest(root / "tests" / "test-groups.toml")
    serial = manifest.serial_files()
    all_tests = sorted(f"tests/{p.name}" for p in (root / "tests").glob("test_*.py"))
    serial_tests = [f for f in all_tests if f in serial]
    parallel_tests = [f for f in all_tests if f not in serial]

    if coverage_file.exists():
        coverage_file.unlink()
    first = True
    if serial_tests:
        _run_pytest(root, coverage_file, serial_tests, xdist=False, append=not first)
        first = False
    if parallel_tests:
        _run_pytest(root, coverage_file, parallel_tests, xdist=True, append=not first)


def _function_body_lines(source: str) -> set[int]:
    """Line numbers that execute only when a function is *called*, not at import.

    Excludes module-level statements, imports, and class/def signatures +
    decorators (all of which run at import time in every test that imports the
    package, inflating the blast radius). Keeping only function-body lines makes
    the map reflect which tests actually *exercise* a module's logic.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    body_lines: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            for stmt in node.body:
                start = getattr(stmt, "lineno", None)
                end = getattr(stmt, "end_lineno", start)
                if start is not None and end is not None:
                    body_lines.update(range(start, end + 1))
    return body_lines


def build_map(root: Path, coverage_file: Path) -> dict[str, list[str]]:
    import coverage

    cov = coverage.Coverage(data_file=str(coverage_file))
    cov.load()
    data = cov.get_data()
    src_to_tests: dict[str, set[str]] = {}
    for measured in data.measured_files():
        rel = os.path.relpath(measured, root)
        if not rel.startswith("src/repoforge/") or not rel.endswith(".py"):
            continue
        rel = rel.replace(os.sep, "/")
        try:
            body_lines = _function_body_lines(Path(measured).read_text(encoding="utf-8"))
        except OSError:
            continue
        tests: set[str] = set()
        for lineno, contexts in data.contexts_by_lineno(measured).items():
            if lineno not in body_lines:
                continue  # import/def/class-level line -> runs at import, not on call
            for context in contexts:
                if context and "::" in context:
                    tests.add(context.split("::", 1)[0].replace(os.sep, "/"))
        if tests:
            src_to_tests[rel] = tests
    return {src: sorted(tests) for src, tests in sorted(src_to_tests.items())}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--map-path", type=Path, default=DEFAULT_MAP_PATH)
    parser.add_argument(
        "--coverage-file",
        type=Path,
        default=Path(".cache/coverage-map.coverage"),
        help="Where the intermediate coverage data is written",
    )
    parser.add_argument(
        "--from-existing-coverage",
        action="store_true",
        help="Skip the pytest run; invert an already-recorded coverage file",
    )
    args = parser.parse_args(argv)
    root = args.root.resolve()
    coverage_file = (root / args.coverage_file).resolve()
    coverage_file.parent.mkdir(parents=True, exist_ok=True)

    if not args.from_existing_coverage:
        _record_coverage(root, coverage_file)

    mapping = build_map(root, coverage_file)
    if not mapping:
        print(
            "[build-coverage-map] no coverage recorded; refusing to write empty map",
            file=sys.stderr,
        )
        return 1
    map_path = root / args.map_path
    map_path.write_text(json.dumps(mapping, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    total_tests = len({t for tests in mapping.values() for t in tests})
    print(
        f"[build-coverage-map] wrote {map_path} "
        f"({len(mapping)} source files -> {total_tests} test files)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
