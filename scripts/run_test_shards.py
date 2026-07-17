#!/usr/bin/env python3
"""Run deterministic pytest shards and combine branch coverage."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ShardResult:
    index: int
    files: tuple[Path, ...]
    returncode: int
    stdout: str
    stderr: str


def order_shard_results(results: tuple[ShardResult, ...]) -> tuple[ShardResult, ...]:
    """Report failed shards first so bounded callers retain the actionable tail."""
    return tuple(sorted(results, key=lambda result: (result.returncode == 0, result.index)))


def partition_test_files(
    test_files: list[Path] | tuple[Path, ...], shard_count: int
) -> tuple[tuple[Path, ...], ...]:
    """Partition tests deterministically using file size as a stable cost proxy."""
    if not isinstance(shard_count, int) or isinstance(shard_count, bool) or shard_count <= 0:
        raise ValueError("shard_count must be a positive integer")
    if not test_files:
        return ()

    unique = {path.resolve(): path for path in test_files}
    ordered = sorted(
        unique.values(),
        key=lambda path: (-path.stat().st_size, path.as_posix()),
    )
    actual_count = min(shard_count, len(ordered))
    buckets: list[list[Path]] = [[] for _ in range(actual_count)]
    weights = [0] * actual_count
    for path in ordered:
        index = min(range(actual_count), key=lambda item: (weights[item], item))
        buckets[index].append(path)
        weights[index] += path.stat().st_size
    return tuple(tuple(sorted(bucket, key=lambda path: path.as_posix())) for bucket in buckets)


def _run_shard(
    root: Path,
    coverage_dir: Path,
    index: int,
    files: tuple[Path, ...],
) -> ShardResult:
    coverage_file = coverage_dir / f".coverage.shard-{index:02d}"
    environment = dict(os.environ)
    environment["COVERAGE_FILE"] = str(coverage_file)
    command = [
        sys.executable,
        "-m",
        "pytest",
        "--timeout=60",
        "-p",
        "no:cacheprovider",
        "--cov=repoforge",
        "--cov-branch",
        "--cov-report=",
        "--cov-fail-under=0",
        *(str(path.relative_to(root)) for path in files),
    ]
    completed = subprocess.run(
        command,
        cwd=root,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    return ShardResult(
        index=index,
        files=files,
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


def _emit_result(result: ShardResult) -> None:
    names = ", ".join(path.name for path in result.files)
    print(f"[pytest-shard {result.index}] files: {names}")
    if result.stdout:
        print(result.stdout, end="" if result.stdout.endswith("\n") else "\n")
    if result.stderr:
        print(result.stderr, file=sys.stderr, end="" if result.stderr.endswith("\n") else "\n")


def _run_coverage_command(
    root: Path,
    coverage_dir: Path,
    arguments: list[str],
) -> int:
    environment = dict(os.environ)
    environment["COVERAGE_FILE"] = str(coverage_dir / ".coverage")
    completed = subprocess.run(
        [sys.executable, "-m", "coverage", *arguments],
        cwd=root,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.stdout:
        print(completed.stdout, end="" if completed.stdout.endswith("\n") else "\n")
    if completed.stderr:
        print(
            completed.stderr, file=sys.stderr, end="" if completed.stderr.endswith("\n") else "\n"
        )
    return completed.returncode


def run(root: Path, coverage_dir: Path, shard_count: int) -> int:
    root = root.resolve()
    coverage_dir = coverage_dir.resolve()
    tests = sorted((root / "tests").rglob("test_*.py"))
    shards = partition_test_files(tests, shard_count)
    if not shards:
        print("no pytest files found", file=sys.stderr)
        return 2

    coverage_dir.mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(max_workers=len(shards)) as executor:
        futures = [
            executor.submit(_run_shard, root, coverage_dir, index, files)
            for index, files in enumerate(shards, start=1)
        ]
        results = order_shard_results(tuple(future.result() for future in futures))

    failed = False
    for result in results:
        _emit_result(result)
        failed = failed or result.returncode != 0
    if failed:
        return 1

    if _run_coverage_command(root, coverage_dir, ["combine", "--keep", str(coverage_dir)]):
        return 1
    return _run_coverage_command(
        root,
        coverage_dir,
        ["report", "--show-missing", "--fail-under=80"],
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--coverage-dir", type=Path, required=True)
    parser.add_argument(
        "--shards",
        type=int,
        default=int(os.environ.get("REPOFORGE_TEST_SHARDS", "4")),
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        return run(args.root, args.coverage_dir, args.shards)
    except (OSError, ValueError) as exc:
        print(f"test sharding failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
