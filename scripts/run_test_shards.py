#!/usr/bin/env python3
"""Run deterministic pytest shards and combine branch coverage."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import select_affected_tests as selector

_DURATION_LINE = re.compile(r"^\s*([\d.]+)s\s+(?:call|setup|teardown)\s+(\S+)")


@dataclass(frozen=True, slots=True)
class ShardResult:
    index: int
    files: tuple[Path, ...]
    returncode: int
    stdout: str
    stderr: str


def order_shard_results(results: tuple[ShardResult, ...]) -> tuple[ShardResult, ...]:
    """Report failed shards first so bounded callers retain actionable output."""
    return tuple(sorted(results, key=lambda result: (result.returncode == 0, result.index)))


def failure_summary(results: tuple[ShardResult, ...]) -> str:
    """Render compact failing pytest nodes at the final output tail."""
    lines = ["[pytest-shard-summary]"]
    for result in sorted(results, key=lambda item: item.index):
        if result.returncode == 0:
            continue
        output_lines = (*result.stdout.splitlines(), *result.stderr.splitlines())
        actionable = [
            line.strip() for line in output_lines if line.lstrip().startswith(("FAILED ", "ERROR "))
        ]
        details = [line.rstrip() for line in output_lines if line.startswith("E   ")][:8]
        if actionable:
            lines.extend(f"shard {result.index}: {line}" for line in actionable)
            lines.extend(f"shard {result.index} detail: {line}" for line in details)
        else:
            lines.append(f"shard {result.index}: failed with exit code {result.returncode}")
            lines.extend(f"shard {result.index} detail: {line}" for line in details)
    return "\n".join(lines) + "\n"


def partition_test_files(
    test_files: list[Path] | tuple[Path, ...],
    shard_count: int,
    weights: dict[Path, float] | None = None,
) -> tuple[tuple[Path, ...], ...]:
    """Partition tests deterministically, weighting by recorded duration when available.

    Falls back to file size (a stable cost proxy) for any file with no recorded duration,
    which also preserves the original size-only behavior when ``weights`` is omitted.
    """
    if not isinstance(shard_count, int) or isinstance(shard_count, bool) or shard_count <= 0:
        raise ValueError("shard_count must be a positive integer")
    if not test_files:
        return ()

    unique = {path.resolve(): path for path in test_files}

    def weight_of(path: Path) -> float:
        if weights is not None:
            recorded = weights.get(path.resolve())
            if recorded is not None:
                return recorded
        return float(path.stat().st_size)

    ordered = sorted(
        unique.values(),
        key=lambda path: (-weight_of(path), path.as_posix()),
    )
    actual_count = min(shard_count, len(ordered))
    buckets: list[list[Path]] = [[] for _ in range(actual_count)]
    bucket_weights = [0.0] * actual_count
    for path in ordered:
        index = min(range(actual_count), key=lambda item: (bucket_weights[item], item))
        buckets[index].append(path)
        bucket_weights[index] += weight_of(path)
    return tuple(tuple(sorted(bucket, key=lambda path: path.as_posix())) for bucket in buckets)


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
    except (OSError, ValueError, KeyError) as exc:
        print(f"[warn] could not load {manifest_path}: {exc}", file=sys.stderr)
        return set()
    serial: set[Path] = set()
    for group in manifest.groups:
        if group.parallel:
            continue
        for relative in group.test_files:
            serial.add((root / relative).resolve())
    return serial


def _parse_durations(stdout: str) -> dict[str, float]:
    """Aggregate per-file wall-clock seconds from a ``--durations=0`` pytest report."""
    totals: dict[str, float] = {}
    for line in stdout.splitlines():
        match = _DURATION_LINE.match(line)
        if not match:
            continue
        seconds = float(match.group(1))
        node_id = match.group(2)
        file_part = node_id.split("::", 1)[0]
        totals[file_part] = totals.get(file_part, 0.0) + seconds
    return totals


def _load_timing_weights(timing_file: Path, root: Path) -> dict[Path, float] | None:
    if not timing_file.exists():
        return None
    try:
        raw = json.loads(timing_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    weights = {(root / relative).resolve(): float(seconds) for relative, seconds in raw.items()}
    return weights or None


def _persist_timings(timing_file: Path, results: tuple[ShardResult, ...], root: Path) -> None:
    aggregated: dict[str, float] = {}
    for result in results:
        for file_part, seconds in _parse_durations(result.stdout).items():
            aggregated[file_part] = aggregated.get(file_part, 0.0) + seconds
    if not aggregated:
        return
    existing: dict[str, float] = {}
    if timing_file.exists():
        try:
            loaded = json.loads(timing_file.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                existing = loaded
        except (OSError, json.JSONDecodeError):
            existing = {}
    existing.update(aggregated)
    timing_file.parent.mkdir(parents=True, exist_ok=True)
    timing_file.write_text(json.dumps(existing, sort_keys=True, indent=2) + "\n", encoding="utf-8")


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
        "--durations=0",
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


def run(
    root: Path,
    coverage_dir: Path,
    shard_count: int,
    *,
    timing_file: Path | None = None,
) -> int:
    root = root.resolve()
    coverage_dir = coverage_dir.resolve()
    tests = sorted((root / "tests").rglob("test_*.py"))
    if not tests:
        print("no pytest files found", file=sys.stderr)
        return 2

    serial_files = _load_serial_test_files(root)
    serial_tests = tuple(path for path in tests if path.resolve() in serial_files)
    parallel_tests = tuple(path for path in tests if path.resolve() not in serial_files)

    coverage_dir.mkdir(parents=True, exist_ok=True)
    weights = _load_timing_weights(timing_file, root) if timing_file is not None else None

    results: list[ShardResult] = []
    next_index = 1

    # Stateful (non-parallel) groups run alone, sequentially, before the parallel
    # phase starts: they must never run concurrently with one another, and running
    # them in their own isolated phase (rather than as just one more concurrent
    # shard) avoids the same worker-contention risk that made -n 4 xdist flaky.
    if serial_tests:
        serial_result = _run_shard(root, coverage_dir, next_index, serial_tests)
        results.append(serial_result)
        next_index += 1

    if parallel_tests:
        parallel_shards = partition_test_files(parallel_tests, shard_count, weights)
        with ThreadPoolExecutor(max_workers=len(parallel_shards)) as executor:
            futures = [
                executor.submit(_run_shard, root, coverage_dir, next_index + offset, files)
                for offset, files in enumerate(parallel_shards)
            ]
            results.extend(future.result() for future in futures)

    ordered = order_shard_results(tuple(results))
    failed = False
    for result in ordered:
        _emit_result(result)
        failed = failed or result.returncode != 0

    if timing_file is not None:
        _persist_timings(timing_file, ordered, root)

    if failed:
        print(failure_summary(ordered), end="")
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
    parser.add_argument(
        "--timing-file",
        type=Path,
        default=Path(".cache/test-shard-timings.json"),
        help="Durable per-file duration cache used to balance parallel shards",
    )
    parser.add_argument(
        "--no-timing",
        action="store_true",
        help="Disable timing-aware balancing (fall back to file-size balancing)",
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    timing_file = None if args.no_timing else args.timing_file
    try:
        return run(args.root, args.coverage_dir, args.shards, timing_file=timing_file)
    except (OSError, ValueError) as exc:
        print(f"test sharding failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
