from __future__ import annotations

import importlib.util
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest


def _load_runner_module() -> Any:
    script = Path(__file__).parents[1] / "scripts/run_test_shards.py"
    spec = importlib.util.spec_from_file_location("repoforge_run_test_shards", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


runner_module = _load_runner_module()
partition_test_files: Callable[[list[Path] | tuple[Path, ...], int], Any] = (
    runner_module.partition_test_files
)


def _sized_file(root: Path, name: str, size: int) -> Path:
    path = root / name
    path.write_text("x" * size, encoding="utf-8")
    return path


def test_partition_test_files_is_deterministic_complete_and_balanced(tmp_path: Path) -> None:
    files = [
        _sized_file(tmp_path, "test_large.py", 100),
        _sized_file(tmp_path, "test_medium.py", 70),
        _sized_file(tmp_path, "test_small_a.py", 20),
        _sized_file(tmp_path, "test_small_b.py", 10),
    ]

    first = partition_test_files(files, 2)
    second = partition_test_files(list(reversed(files)), 2)

    assert first == second
    assert sorted(path for shard in first for path in shard) == sorted(files)
    assert all(first)
    weights = [sum(path.stat().st_size for path in shard) for shard in first]
    assert max(weights) - min(weights) <= 40


def test_partition_test_files_rejects_invalid_shard_count(tmp_path: Path) -> None:
    test_file = _sized_file(tmp_path, "test_one.py", 1)
    with pytest.raises(ValueError, match="shard_count"):
        partition_test_files([test_file], 0)


def test_failed_shard_results_are_reported_before_successes() -> None:
    passed_first = runner_module.ShardResult(1, (), 0, "passed one", "")
    failed = runner_module.ShardResult(2, (), 1, "failed two", "")
    passed_last = runner_module.ShardResult(3, (), 0, "passed three", "")

    ordered = runner_module.order_shard_results((passed_last, failed, passed_first))

    assert [result.index for result in ordered] == [2, 1, 3]


def test_failed_shard_summary_keeps_actionable_pytest_nodes_at_tail() -> None:
    failed = runner_module.ShardResult(
        2,
        (),
        1,
        "E   Failed: Timeout (>60.0s) from pytest-timeout.\n"
        "E   AssertionError: expected exact contract\n"
        "FAILED tests/test_alpha.py::test_one - AssertionError\n"
        "ERROR tests/test_beta.py::test_two - RuntimeError\n",
        "worker warning\n",
    )
    opaque = runner_module.ShardResult(3, (), 2, "collection interrupted\n", "")
    passed = runner_module.ShardResult(1, (), 0, "12 passed\n", "")

    summary = runner_module.failure_summary((passed, failed, opaque))

    assert summary == (
        "[pytest-shard-summary]\n"
        "shard 2: FAILED tests/test_alpha.py::test_one - AssertionError\n"
        "shard 2: ERROR tests/test_beta.py::test_two - RuntimeError\n"
        "shard 2 detail: E   Failed: Timeout (>60.0s) from pytest-timeout.\n"
        "shard 2 detail: E   AssertionError: expected exact contract\n"
        "shard 3: failed with exit code 2\n"
    )


def test_partition_test_files_uses_recorded_weights_over_file_size(tmp_path: Path) -> None:
    # All four files are the same byte size, so a size-based partition would be
    # arbitrary; recorded durations should dominate the balance instead.
    files = [
        _sized_file(tmp_path, "test_a.py", 10),
        _sized_file(tmp_path, "test_b.py", 10),
        _sized_file(tmp_path, "test_c.py", 10),
        _sized_file(tmp_path, "test_d.py", 10),
    ]
    weights = {
        files[0].resolve(): 100.0,
        files[1].resolve(): 1.0,
        files[2].resolve(): 1.0,
        files[3].resolve(): 1.0,
    }

    shards = partition_test_files(files, 2, weights)

    heavy_shard = next(shard for shard in shards if files[0] in shard)
    assert len(heavy_shard) == 1, "the heaviest file should not share a shard with any other file"


def test_partition_test_files_falls_back_to_size_for_unweighted_files(tmp_path: Path) -> None:
    files = [
        _sized_file(tmp_path, "test_large.py", 100),
        _sized_file(tmp_path, "test_small.py", 10),
    ]
    weights = {files[0].resolve(): 5.0}  # only one file has a recorded duration

    shards = partition_test_files(files, 2, weights)

    assert sorted(path for shard in shards for path in shard) == sorted(files)


def test_parse_durations_aggregates_seconds_per_test_file() -> None:
    stdout = (
        "=============== slowest durations ===============\n"
        "1.50s call     tests/test_alpha.py::test_one\n"
        "0.25s setup    tests/test_alpha.py::test_one\n"
        "0.75s call     tests/test_beta.py::test_two[param]\n"
        "(0.00 durations hidden.  Use -vv to show these durations.)\n"
    )

    totals = runner_module._parse_durations(stdout)

    assert totals == {
        "tests/test_alpha.py": pytest.approx(1.75),
        "tests/test_beta.py": pytest.approx(0.75),
    }


def test_timing_round_trips_through_persist_and_load(tmp_path: Path) -> None:
    root = tmp_path
    (root / "tests").mkdir()
    timing_file = root / ".cache" / "test-shard-timings.json"
    results = (
        runner_module.ShardResult(1, (), 0, "2.00s call     tests/test_alpha.py::test_one\n", ""),
    )

    runner_module._persist_timings(timing_file, results, root)
    weights = runner_module._load_timing_weights(timing_file, root)

    assert weights == {(root / "tests/test_alpha.py").resolve(): pytest.approx(2.0)}

    # A second run for a different file should merge, not clobber, prior entries.
    more_results = (
        runner_module.ShardResult(1, (), 0, "3.00s call     tests/test_beta.py::test_two\n", ""),
    )
    runner_module._persist_timings(timing_file, more_results, root)
    merged = runner_module._load_timing_weights(timing_file, root)
    assert merged is not None
    assert (root / "tests/test_alpha.py").resolve() in merged
    assert (root / "tests/test_beta.py").resolve() in merged


def test_load_serial_test_files_honors_manifest_parallel_flag(tmp_path: Path) -> None:
    root = tmp_path
    (root / "tests").mkdir()
    manifest = root / "tests" / "test-groups.toml"
    manifest.write_text(
        """
[groups.narrow]
description = "x"
parallel = false
source_globs = []
test_files = ["tests/test_serial_one.py"]

[groups.wide]
description = "x"
parallel = true
source_globs = []
test_files = ["tests/test_parallel_one.py"]
""",
        encoding="utf-8",
    )

    serial = runner_module._load_serial_test_files(root)

    assert serial == {(root / "tests/test_serial_one.py").resolve()}


def test_load_serial_test_files_defaults_to_empty_when_manifest_missing(tmp_path: Path) -> None:
    assert runner_module._load_serial_test_files(tmp_path) == set()
