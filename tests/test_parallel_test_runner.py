from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest


def _load_runner_module() -> Any:
    script = Path(__file__).parents[1] / "scripts/run_test_suite.py"
    spec = importlib.util.spec_from_file_location("repoforge_run_test_suite", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


runner_module = _load_runner_module()


def _lane(tmp_path: Path, name: str, files: tuple[str, ...], *, workers: int | None):
    root = tmp_path
    return runner_module.lane_command(
        root,
        tmp_path / "coverage",
        name,
        tuple(root / item for item in files),
        workers=workers,
    )


def test_serial_lane_runs_without_xdist(tmp_path: Path) -> None:
    """The serial lane exists to keep shared-state tests out of any `-n` process."""
    command, _ = _lane(tmp_path, "serial", ("tests/test_one.py",), workers=None)

    assert "-n" not in command
    assert command[-1] == "tests/test_one.py"


def test_parallel_lane_runs_under_xdist_at_the_requested_worker_count(tmp_path: Path) -> None:
    command, _ = _lane(tmp_path, "xdist", ("tests/test_one.py",), workers=3)

    assert command[command.index("-n") + 1] == "3"


def test_default_worker_count_is_the_measured_machine_cap() -> None:
    """`-n 4` crashed a worker and produced contention-flaky failures; 3 is the cap."""
    assert runner_module.DEFAULT_WORKERS == 3


def test_lanes_write_separate_coverage_data_files(tmp_path: Path) -> None:
    """Both lanes must survive into the combine step, or the floor is measured on half."""
    _, serial_env = _lane(tmp_path, "serial", ("tests/test_one.py",), workers=None)
    _, xdist_env = _lane(tmp_path, "xdist", ("tests/test_two.py",), workers=3)

    assert serial_env["COVERAGE_FILE"] != xdist_env["COVERAGE_FILE"]
    for environment in (serial_env, xdist_env):
        assert Path(environment["COVERAGE_FILE"]).name.startswith(".coverage.")


def test_coverage_lane_records_per_test_context(tmp_path: Path) -> None:
    command, _ = _lane(tmp_path, "xdist", ("tests/test_one.py",), workers=3)

    assert "--cov-context=test" in command


def test_lane_does_not_override_the_repository_pytest_timeout(tmp_path: Path) -> None:
    """pyproject's 120s clears the app's git subprocess budget; a lower one kills healthy calls."""
    command, _ = _lane(tmp_path, "xdist", ("tests/test_one.py",), workers=3)

    assert not any(argument.startswith("--timeout") for argument in command)


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


def test_missing_manifest_refuses_to_build_a_parallel_lane(tmp_path: Path) -> None:
    with pytest.raises(runner_module.SelectionMetadataError, match="missing"):
        runner_module.load_lane_plan(tmp_path)


def test_unreadable_manifest_refuses_to_build_a_parallel_lane(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test-groups.toml").write_text("not = [valid", encoding="utf-8")

    with pytest.raises(runner_module.SelectionMetadataError, match="invalid"):
        runner_module.load_lane_plan(tmp_path)


def test_full_runner_writes_lane_failure_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    test_file = tmp_path / "tests" / "test_one.py"
    test_file.parent.mkdir()
    test_file.write_text("def test_one(): pass\n", encoding="utf-8")
    monkeypatch.setattr(
        runner_module,
        "load_lane_plan",
        lambda _root: runner_module.LanePlan(serial=(), parallel=(test_file,)),
    )
    monkeypatch.setattr(runner_module, "_run_lane", lambda *_args, **_kwargs: 1)
    monkeypatch.setattr(runner_module, "_head_sha", lambda _root: "a" * 40)
    report = tmp_path / "full-report.json"

    returncode = runner_module.run(tmp_path, None, 3, report_path=report)

    payload = json.loads(report.read_text(encoding="utf-8"))
    assert returncode == 1
    assert payload["intent"] == "full"
    assert payload["lanes"][0]["returncode"] == 1
