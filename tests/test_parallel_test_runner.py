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
