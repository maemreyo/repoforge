#!/usr/bin/env python3
"""Run production-composition control-plane fault selectors and publish gate evidence."""

from __future__ import annotations

import argparse
import json
import platform
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import repoforge  # noqa: E402
from repoforge.benchmark.control_plane import (  # noqa: E402
    ControlPlaneIdentity,
    ControlPlaneScenario,
    ScenarioExecution,
    evaluate_control_plane_gates,
    load_control_plane_manifest,
    publish_control_plane_report,
    run_control_plane_scenarios,
)
from repoforge.contracts.generated_contract_identity import CONTRACT_IDENTITY  # noqa: E402

DEFAULT_MANIFEST = ROOT / "tests" / "fixtures" / "v2_corpora" / "control_plane_faults.json"
DEFAULT_REPORT_DIR = ROOT / "build" / "reports"
_MAX_EXCERPT_CHARS = 8_000


def _run_text(argv: list[str]) -> str:
    result = subprocess.run(
        argv,
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _identity() -> ControlPlaneIdentity:
    release_contract = json.loads(
        (ROOT / "docs" / "contracts" / "release-contract-v2.json").read_text(encoding="utf-8")
    )
    mcp = release_contract["mcp"]
    dirty = bool(_run_text(["git", "status", "--porcelain", "--untracked-files=normal"]))
    return ControlPlaneIdentity(
        git_head=_run_text(["git", "rev-parse", "HEAD"]),
        dirty=dirty,
        python_version=platform.python_version(),
        package_version=repoforge.__version__,
        contract_version=str(mcp["identity"]),
        tool_count=int(CONTRACT_IDENTITY["tool_count"]),
        tool_surface_hash=str(mcp["tool_surface_hash"]),
        schema_bundle_digest=str(CONTRACT_IDENTITY["tool_schema_bundle_digest"]),
    )


def _execute(scenario: ControlPlaneScenario) -> ScenarioExecution:
    started = time.perf_counter()
    result = subprocess.run(
        ["uv", "run", "--extra", "dev", "pytest", scenario.selector, "-q"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    output = "\n".join(part for part in (result.stdout.strip(), result.stderr.strip()) if part)
    return ScenarioExecution(
        scenario_id=scenario.scenario_id,
        selector=scenario.selector,
        passed=result.returncode == 0,
        duration_ms=round((time.perf_counter() - started) * 1_000.0, 3),
        attempts=1,
        output_excerpt=output[-_MAX_EXCERPT_CHARS:],
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--report-dir", type=Path, default=DEFAULT_REPORT_DIR)
    args = parser.parse_args()

    try:
        manifest = load_control_plane_manifest(args.manifest)
        executions = run_control_plane_scenarios(manifest, _execute)
        report = evaluate_control_plane_gates(manifest, executions, identity=_identity())
        paths = publish_control_plane_report(report, args.report_dir)
    except (OSError, subprocess.CalledProcessError, TypeError, ValueError) as exc:
        print(f"Control-plane fault gates could not run: {exc}", file=sys.stderr)
        return 2

    print(f"report: {paths.json_path}")
    print(f"summary: {paths.markdown_path}")
    for execution in report.executions:
        print(
            f"{execution.scenario_id}: {'passed' if execution.passed else 'failed'} "
            f"attempts={execution.attempts} duration_ms={execution.duration_ms:.3f}"
        )
    print(
        "metrics: "
        f"unknown_effect_outcomes={report.metrics.unknown_effect_outcomes} "
        f"calls_per_completed_task={report.metrics.calls_per_completed_task:.3f} "
        f"duplicate_read_rate={report.metrics.duplicate_read_rate:.3%} "
        f"hidden_retries={report.hidden_retry_count}"
    )
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
