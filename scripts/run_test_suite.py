#!/usr/bin/env python3
"""Run the whole pytest suite in two lanes and report combined branch coverage.

This used to partition the suite across four independent pytest processes, balanced from a
recorded-duration cache. That was measured to be far slower than running the same tests
under xdist, because these tests are dominated by git subprocess spawns: four concurrent
pytest processes oversubscribe a machine whose measured stable worker cap is three, and a
fixed partition cannot rebalance when one side runs long. One file recorded at 322.8s
inside that runner took 33s alone under identical coverage flags.

The lanes here are the same split `select_affected_tests.py` uses, which is what `make
test-fast` runs:

* the serial lane holds `parallel = false` groups from `tests/test-groups.toml` -- genuine
  shared-state tests (fixed-path sockets, runtime state) that must never share a process
  with `-n`; they run first, alone;
* the xdist lane runs everything else in one pytest process at the measured worker cap.

Each lane writes its own coverage data file so the two can be combined at the end.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

import tomllib

sys.path.insert(0, str(Path(__file__).resolve().parent))
import select_affected_tests as selector

DEFAULT_WORKERS = 3
"""Measured cap for this suite: `-n 4` crashed a worker and produced contention-flaky
failures, and the git child processes these tests spawn oversubscribe cores well before
pytest itself does."""

COVERAGE_FLOOR = "80"


def _load_serial_test_files(root: Path) -> set[Path]:
    """Resolve absolute paths of test files in non-parallel (serial-lane) groups.

    Falls back to an empty set (all files parallel-eligible, matching prior behavior)
    when the manifest is missing or invalid, rather than failing the whole run.
    """
    manifest_path = root / "tests" / "test-groups.toml"
    if not manifest_path.exists():
        return set()
    try:
        manifest = selector.load_manifest(manifest_path)
    except (OSError, ValueError, KeyError, tomllib.TOMLDecodeError) as exc:
        print(f"[warn] could not load {manifest_path}: {exc}", file=sys.stderr)
        return set()
    serial: set[Path] = set()
    for group in manifest.groups:
        if group.parallel:
            continue
        for relative in group.test_files:
            serial.add((root / relative).resolve())
    return serial


def lane_command(
    root: Path,
    coverage_dir: Path,
    name: str,
    files: tuple[Path, ...],
    *,
    workers: int | None,
) -> tuple[list[str], dict[str, str]]:
    """Build one lane's pytest argv and environment."""
    environment = dict(os.environ)
    environment["COVERAGE_FILE"] = str(coverage_dir / f".coverage.lane-{name}")
    command = [
        sys.executable,
        "-m",
        "pytest",
        # No --timeout here on purpose: pyproject sets 120 to clear the app's own git
        # subprocess budget, and a lower ceiling kills healthy-but-slow git calls under
        # contention -- the inversion 546cf52 removed.
        "-p",
        "no:cacheprovider",
        "-q",
        "--cov=repoforge",
        "--cov-branch",
        "--cov-report=",
        "--cov-fail-under=0",
    ]
    if workers is not None:
        command.extend(["-n", str(workers)])
    command.extend(str(path.relative_to(root)) for path in files)
    return command, environment


def _run_lane(
    root: Path,
    coverage_dir: Path,
    name: str,
    files: tuple[Path, ...],
    *,
    workers: int | None,
) -> int:
    """Run one lane, streaming pytest's own output so a long gate stays observable."""
    command, environment = lane_command(root, coverage_dir, name, files, workers=workers)
    print(f"[test-suite] {name} lane: {len(files)} test files", flush=True)
    return subprocess.run(command, cwd=root, env=environment, check=False).returncode


def _run_coverage_command(root: Path, coverage_dir: Path, arguments: list[str]) -> int:
    environment = dict(os.environ)
    environment["COVERAGE_FILE"] = str(coverage_dir / ".coverage")
    completed = subprocess.run(
        [sys.executable, "-m", "coverage", *arguments],
        cwd=root,
        env=environment,
        check=False,
    )
    return completed.returncode


def run(root: Path, coverage_dir: Path, workers: int) -> int:
    root = root.resolve()
    coverage_dir = coverage_dir.resolve()
    tests = sorted((root / "tests").rglob("test_*.py"))
    if not tests:
        print("no pytest files found", file=sys.stderr)
        return 2

    serial_files = _load_serial_test_files(root)
    serial = tuple(path for path in tests if path.resolve() in serial_files)
    parallel = tuple(path for path in tests if path.resolve() not in serial_files)

    coverage_dir.mkdir(parents=True, exist_ok=True)

    failed = False
    if serial:
        failed = _run_lane(root, coverage_dir, "serial", serial, workers=None) != 0
    if parallel:
        # Both lanes always run: a serial-lane failure must not hide the rest of the suite,
        # which is what the gate is being asked to report on.
        failed = _run_lane(root, coverage_dir, "xdist", parallel, workers=workers) != 0 or failed

    if failed:
        return 1
    if _run_coverage_command(root, coverage_dir, ["combine", "--keep", str(coverage_dir)]):
        return 1
    return _run_coverage_command(
        root,
        coverage_dir,
        ["report", "--show-missing", f"--fail-under={COVERAGE_FLOOR}"],
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--coverage-dir", type=Path, required=True)
    parser.add_argument(
        "--workers",
        type=int,
        default=int(os.environ.get("REPOFORGE_TEST_WORKERS", str(DEFAULT_WORKERS))),
        help=f"xdist workers for the parallel lane (default {DEFAULT_WORKERS}, the measured cap)",
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.workers < 1:
        print("--workers must be a positive integer", file=sys.stderr)
        return 2
    try:
        return run(args.root, args.coverage_dir, args.workers)
    except (OSError, ValueError) as exc:
        print(f"test suite run failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
