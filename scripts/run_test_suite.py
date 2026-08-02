#!/usr/bin/env python3
"""Run the complete pytest suite with validated serial and parallel lanes.

Without ``--coverage-dir`` this runs the full behavioral suite without coverage.
With ``--coverage-dir`` it records and enforces branch coverage. Lane metadata is
correctness-critical: missing or invalid metadata is refused before pytest starts.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import tomli as tomllib

sys.path.insert(0, str(Path(__file__).resolve().parent))
import select_affected_tests as selector
from verification_artifact import LaneTiming, VerificationArtifact, write_artifact

DEFAULT_WORKERS = 3
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
    environment = dict(os.environ)
    command = [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q"]
    if coverage_dir is not None:
        environment["COVERAGE_FILE"] = str(coverage_dir / f".coverage.lane-{name}")
        command.extend(
            [
                "--cov=repoforge",
                "--cov-branch",
                "--cov-context=test",
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
    command, environment = lane_command(root, coverage_dir, name, files, workers=workers)
    print(f"[test-suite] {name} lane: {len(files)} test files", flush=True)
    return subprocess.run(command, cwd=root, env=environment, check=False).returncode


def _run_coverage_command(root: Path, coverage_dir: Path, arguments: list[str]) -> int:
    environment = dict(os.environ)
    environment["COVERAGE_FILE"] = str(coverage_dir / ".coverage")
    return subprocess.run(
        [sys.executable, "-m", "coverage", *arguments],
        cwd=root,
        env=environment,
        check=False,
    ).returncode


def _head_sha(root: Path) -> str:
    return (
        subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        )
        .stdout.strip()
        .lower()
    )


def run(
    root: Path,
    coverage_dir: Path | None,
    workers: int,
    *,
    report_path: Path | None = None,
) -> int:
    root = root.resolve()
    plan = load_lane_plan(root)
    resolved_coverage_dir = coverage_dir.resolve() if coverage_dir is not None else None
    if resolved_coverage_dir is not None:
        resolved_coverage_dir.mkdir(parents=True, exist_ok=True)

    failed = False
    timings: list[LaneTiming] = []
    for name, files, lane_workers in (
        ("serial", plan.serial, None),
        ("parallel", plan.parallel, workers),
    ):
        if not files:
            continue
        started = time.monotonic()
        returncode = _run_lane(
            root,
            resolved_coverage_dir,
            "xdist" if name == "parallel" else name,
            files,
            workers=lane_workers,
        )
        timings.append(
            LaneTiming(name, len(files), (time.monotonic() - started) * 1_000, returncode)
        )
        if returncode != 0:
            failed = True
            break

    outcome = 1 if failed else 0
    if outcome == 0 and resolved_coverage_dir is not None:
        outcome = _run_coverage_command(
            root,
            resolved_coverage_dir,
            ["combine", "--keep", str(resolved_coverage_dir)],
        )
        if outcome == 0:
            outcome = _run_coverage_command(
                root,
                resolved_coverage_dir,
                ["report", "--show-missing", f"--fail-under={COVERAGE_FLOOR}"],
            )

    if report_path is not None:
        write_artifact(
            report_path,
            VerificationArtifact(
                schema_version=1,
                intent="coverage" if resolved_coverage_dir is not None else "full",
                head_sha=_head_sha(root),
                selected_count=len(plan.serial) + len(plan.parallel),
                escalated=False,
                escalation_reason=None,
                lanes=tuple(timings),
            ),
        )
    return outcome


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
    )
    parser.add_argument(
        "--report-path",
        type=Path,
        default=None,
        help="Write bounded canonical JSON evidence for this full-suite run.",
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.workers < 1:
        print("--workers must be a positive integer", file=sys.stderr)
        return 2
    try:
        return run(
            args.root,
            args.coverage_dir,
            args.workers,
            report_path=args.report_path,
        )
    except (OSError, SelectionMetadataError, ValueError) as exc:
        print(f"test suite run failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
