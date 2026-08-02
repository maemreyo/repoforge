#!/usr/bin/env python3
"""Run the complete pytest suite with validated serial and parallel lanes.

The runner has two explicit modes:

* without ``--coverage-dir`` it runs the full behavioral suite without coverage;
* with ``--coverage-dir`` it records branch coverage for each lane, combines the
  observations, and enforces the repository coverage floor.

Lane metadata is correctness-critical. A missing, unreadable, or incomplete
``tests/test-groups.toml`` is refused before pytest starts; it must never turn a
stateful test into an implicit all-parallel run.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import tomli as tomllib  # stdlib `tomllib` is 3.11+; this package supports 3.10.

sys.path.insert(0, str(Path(__file__).resolve().parent))
import select_affected_tests as selector

DEFAULT_WORKERS = 3
"""Measured cap for this suite: `-n 4` crashed a worker and produced contention-flaky
failures, and the git child processes these tests spawn oversubscribe cores well before
pytest itself does."""

COVERAGE_FLOOR = "80"


@dataclass(frozen=True, slots=True)
class LanePlan:
    """Exact full-suite file partition derived from validated metadata."""

    serial: tuple[Path, ...]
    parallel: tuple[Path, ...]


class SelectionMetadataError(ValueError):
    """The reviewed metadata cannot safely determine test isolation."""


def _load_manifest(root: Path) -> selector.Manifest:
    manifest_path = root / "tests" / "test-groups.toml"
    if not manifest_path.is_file():
        raise SelectionMetadataError(f"selection metadata missing: {manifest_path}")
    try:
        return selector.load_manifest(manifest_path)
    except (OSError, ValueError, KeyError, tomllib.TOMLDecodeError) as exc:
        raise SelectionMetadataError(f"selection metadata invalid: {exc}") from exc


def _load_serial_test_files(root: Path) -> set[Path]:
    """Resolve absolute paths owned by non-parallel groups.

    Kept as a focused helper for callers and tests that only need the reviewed
    isolation projection. Full execution uses :func:`load_lane_plan`, which also
    validates completeness against the files on disk.
    """

    manifest = _load_manifest(root)
    return {
        (root / test_file).resolve()
        for group in manifest.groups
        if not group.parallel
        for test_file in group.test_files
    }


def load_lane_plan(root: Path) -> LanePlan:
    """Build a complete lane plan or fail before widening concurrency."""

    root = root.resolve()
    manifest = _load_manifest(root)
    violations = selector.check_completeness(manifest, root / "tests")
    if violations:
        detail = "; ".join(violations[:10])
        suffix = "" if len(violations) <= 10 else f"; ... and {len(violations) - 10} more"
        raise SelectionMetadataError(f"selection metadata incomplete: {detail}{suffix}")

    tests = tuple(sorted((root / "tests").rglob("test_*.py")))
    if not tests:
        raise SelectionMetadataError("selection metadata has no pytest files to schedule")
    serial_files = {
        (root / test_file).resolve()
        for group in manifest.groups
        if not group.parallel
        for test_file in group.test_files
    }
    return LanePlan(
        serial=tuple(path for path in tests if path.resolve() in serial_files),
        parallel=tuple(path for path in tests if path.resolve() not in serial_files),
    )


def lane_command(
    root: Path,
    coverage_dir: Path | None,
    name: str,
    files: tuple[Path, ...],
    *,
    workers: int | None,
) -> tuple[list[str], dict[str, str]]:
    """Build one lane's pytest argv and environment."""

    environment = dict(os.environ)
    command = [
        sys.executable,
        "-m",
        "pytest",
        # No --timeout here on purpose: pyproject sets 120 to clear the app's own git
        # subprocess budget, and a lower ceiling kills healthy-but-slow git calls under
        # contention.
        "-p",
        "no:cacheprovider",
        "-q",
    ]
    if coverage_dir is not None:
        environment["COVERAGE_FILE"] = str(coverage_dir / f".coverage.lane-{name}")
        command.extend(
            [
                "--cov=repoforge",
                "--cov-branch",
                "--cov-report=",
                "--cov-fail-under=0",
            ]
        )
    if workers is not None:
        command.extend(["-n", str(workers)])
    command.extend(str(path.relative_to(root)) for path in files)
    return command, environment


def _run_lane(
    root: Path,
    coverage_dir: Path | None,
    name: str,
    files: tuple[Path, ...],
    *,
    workers: int | None,
) -> int:
    """Run one lane, streaming pytest output so long gates stay observable."""

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


def run(root: Path, coverage_dir: Path | None, workers: int) -> int:
    root = root.resolve()
    plan = load_lane_plan(root)
    resolved_coverage_dir = coverage_dir.resolve() if coverage_dir is not None else None
    if resolved_coverage_dir is not None:
        resolved_coverage_dir.mkdir(parents=True, exist_ok=True)

    failed = False
    if plan.serial:
        failed = _run_lane(root, resolved_coverage_dir, "serial", plan.serial, workers=None) != 0
    if plan.parallel:
        # Both lanes always run: a serial-lane failure must not hide the rest of the suite.
        failed = (
            _run_lane(root, resolved_coverage_dir, "xdist", plan.parallel, workers=workers) != 0
            or failed
        )

    if failed:
        return 1
    if resolved_coverage_dir is None:
        return 0
    if _run_coverage_command(
        root,
        resolved_coverage_dir,
        ["combine", "--keep", str(resolved_coverage_dir)],
    ):
        return 1
    return _run_coverage_command(
        root,
        resolved_coverage_dir,
        ["report", "--show-missing", f"--fail-under={COVERAGE_FLOOR}"],
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument(
        "--coverage-dir",
        type=Path,
        default=None,
        help="Record and enforce branch coverage in this directory; omit for no coverage.",
    )
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
    except (OSError, SelectionMetadataError, ValueError) as exc:
        print(f"test suite run failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
