from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/production-gate.yml"


def _job(text: str, name: str, next_name: str) -> str:
    return text.split(f"\n  {name}:\n", 1)[1].split(f"\n  {next_name}:\n", 1)[0]


def _load_compatibility_runner() -> Any:
    script = ROOT / "scripts/run_compatibility_tests.py"
    spec = importlib.util.spec_from_file_location("repoforge_compatibility_runner", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_protected_push_has_one_canonical_coverage_job() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "\n  canonical-coverage:\n" in workflow
    assert workflow.count("make coverage") == 1
    assert "test-map-check-existing" in workflow
    assert "\n  coverage-map:\n" not in workflow
    assert "make test-map-check\n" not in workflow


def test_pull_request_has_one_affected_test_job() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    affected = _job(workflow, "affected-tests", "canonical-coverage")

    assert "github.event_name == 'pull_request'" in affected
    assert "select_affected_tests.py" in affected
    assert "--run" in affected
    assert "--shadow-catalog" in affected
    assert "matrix:" not in affected
    assert "--cov" not in affected


def test_compatibility_matrix_is_no_coverage() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    compatibility = _job(workflow, "compatibility", "live-activation")

    assert "run_compatibility_tests.py" in compatibility
    assert "--cov" not in compatibility
    assert "make coverage" not in compatibility
    for version in ("3.10", "3.11", "3.12", "3.13"):
        assert version in compatibility
    assert "macos-latest" in compatibility
    assert "REPOFORGE_CI_FULL_MATRIX" in compatibility


def test_compatibility_runner_has_reviewed_subset_and_full_rollback() -> None:
    runner = _load_compatibility_runner()

    subset = runner.build_command(ROOT, full=False)
    rollback = runner.build_command(ROOT, full=True)

    assert subset[:3] == [sys.executable, "-m", "pytest"]
    assert "--cov" not in subset
    assert "tests/test_onboarding_real_git.py" in subset
    assert "tests/test_repository_discovery.py" in subset
    assert rollback[-2:] == ["--full", "--run"]
    assert "select_affected_tests.py" in rollback[1]


def test_umbrella_gate_names_event_specific_results() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    gate = workflow.split("\n  production-gate:\n", 1)[1]

    for job in (
        "static-contracts",
        "affected-tests",
        "canonical-coverage",
        "compatibility",
        "live-activation",
        "package",
    ):
        assert job in gate
    assert "github.event_name" in gate
    assert "skipped" in gate
