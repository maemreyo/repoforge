from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _benchmark():
    from repoforge.benchmark import harness

    return harness


def _observation(
    corpus: str,
    case_id: str,
    *,
    success: bool = True,
    wrong_target: bool = False,
    regression_caught: bool = False,
    fell_back_full: bool = False,
    truncated: bool = False,
    resume_metadata: bool = True,
):
    harness = _benchmark()
    return harness.CaseObservation(
        corpus=corpus,
        case_id=case_id,
        success=success,
        wrong_target=wrong_target,
        regression_caught=regression_caught,
        fell_back_full=fell_back_full,
        truncated=truncated,
        resume_metadata=resume_metadata,
        duration_ms=1.0,
        details={},
    )


def test_release_gate_thresholds_match_epic_contract() -> None:
    harness = _benchmark()

    assert harness.RELEASE_THRESHOLDS == {
        "generated_changes": 0.99,
        "patches": 0.95,
        "seeded_bugs": 1.0,
        "read_golden": 1.0,
    }


def test_wrong_target_is_a_hard_failure_even_when_success_rate_passes() -> None:
    harness = _benchmark()
    observations = [_observation("generated_changes", f"generated-{index}") for index in range(100)]
    observations[-1] = _observation(
        "generated_changes",
        "generated-99",
        wrong_target=True,
    )

    report = harness.evaluate_release_gates(observations)
    metric = report.metric("generated_changes")

    assert metric.success_rate == 1.0
    assert metric.wrong_target_count == 1
    assert metric.passed is False
    assert report.passed is False


def test_seeded_bug_gate_accepts_detection_or_explicit_full_fallback() -> None:
    harness = _benchmark()
    report = harness.evaluate_release_gates(
        [
            _observation("seeded_bugs", "caught", regression_caught=True),
            _observation("seeded_bugs", "fallback", fell_back_full=True),
        ]
    )

    metric = report.metric("seeded_bugs")
    assert metric.success_count == 2
    assert metric.success_rate == 1.0
    assert metric.passed is True


def test_provider_recall_is_measured_separately_by_provider_and_language() -> None:
    harness = _benchmark()
    metrics = harness.evaluate_provider_recall(
        [
            harness.ProviderRecallObservation(
                provider_id="tree-sitter",
                language="python",
                case_id="python-direct",
                expected_tests=("tests/test_value.py",),
                routed_tests=("tests/test_value.py",),
            ),
            harness.ProviderRecallObservation(
                provider_id="tree-sitter",
                language="typescript",
                case_id="typescript-reexport",
                expected_tests=("src/app.test.tsx",),
                routed_tests=("src/app.test.tsx",),
            ),
            harness.ProviderRecallObservation(
                provider_id="syntax",
                language="typescript",
                case_id="typescript-reexport",
                expected_tests=("src/app.test.tsx",),
                routed_tests=(),
            ),
        ]
    )

    assert [(metric.provider_id, metric.language) for metric in metrics] == [
        ("syntax", "typescript"),
        ("tree-sitter", "python"),
        ("tree-sitter", "typescript"),
    ]
    assert metrics[0].recall == 0.0
    assert metrics[1].recall == 1.0
    assert metrics[2].recall == 1.0
    assert metrics[1].passed is True
    assert metrics[0].passed is False


def test_provider_recall_measurement_skips_unmarked_cases_and_rejects_bad_fixtures() -> None:
    from repoforge.adapters.code_intelligence import TreeSitterCodeIntelligenceProvider
    from repoforge.benchmark.code_intelligence import measure_provider_recall

    harness = _benchmark()
    skipped = harness.CorpusCase(
        corpus="seeded_bugs",
        case_id="not-measured",
        input={},
        expected={},
        metadata={},
    )
    invalid = harness.CorpusCase(
        corpus="seeded_bugs",
        case_id="invalid-fixture",
        input={"files": [], "changed_paths": ["src/value.py"]},
        expected={"candidate_tests": ["tests/test_value.py"]},
        metadata={"provider_recall": True, "language": "python"},
    )

    assert measure_provider_recall(TreeSitterCodeIntelligenceProvider(), (skipped,)) == ()
    with pytest.raises(ValueError, match="must map relative paths"):
        measure_provider_recall(TreeSitterCodeIntelligenceProvider(), (invalid,))


def test_provider_recall_validation_fails_closed() -> None:
    harness = _benchmark()
    empty_expected = harness.ProviderRecallObservation(
        provider_id="tree-sitter",
        language="python",
        case_id="empty",
        expected_tests=(),
        routed_tests=(),
    )
    unknown_provider = harness.ProviderRecallObservation(
        provider_id="unknown",
        language="python",
        case_id="unknown",
        expected_tests=("tests/test_value.py",),
        routed_tests=("tests/test_value.py",),
    )

    with pytest.raises(ValueError, match="require expected tests"):
        harness.evaluate_provider_recall([empty_expected])
    with pytest.raises(ValueError, match="missing a reviewed threshold"):
        harness.evaluate_provider_recall([unknown_provider])


def test_seeded_corpus_calibration_matches_actual_provider_recall() -> None:
    from repoforge.adapters.code_intelligence import (
        SyntaxCodeIntelligenceProvider,
        TreeSitterCodeIntelligenceProvider,
    )
    from repoforge.benchmark.code_intelligence import measure_provider_recall

    harness = _benchmark()
    cases = harness.load_corpus(ROOT / "tests/fixtures/v2_corpora/seeded_bugs.json")
    observations = (
        *measure_provider_recall(TreeSitterCodeIntelligenceProvider(), cases),
        *measure_provider_recall(SyntaxCodeIntelligenceProvider(), cases),
    )
    metrics = harness.evaluate_provider_recall(observations)
    calibration = json.loads(
        (ROOT / "src/repoforge/adapters/code_intelligence/calibration-v1.json").read_text(
            encoding="utf-8"
        )
    )

    assert {(metric.provider_id, metric.language) for metric in metrics} == {
        ("syntax", "javascript"),
        ("syntax", "python"),
        ("syntax", "typescript"),
        ("tree-sitter", "javascript"),
        ("tree-sitter", "python"),
        ("tree-sitter", "typescript"),
    }
    for metric in metrics:
        entry = calibration["providers"][metric.provider_id][metric.language]
        assert entry["cases"] == metric.case_count
        assert entry["routed_test_recall"] == round(metric.recall * 100)


def test_release_report_requires_complete_primary_provider_recall() -> None:
    harness = _benchmark()
    corpus_observations = [
        _observation("generated_changes", "g1"),
        _observation("patches", "p1"),
        _observation("seeded_bugs", "s1", regression_caught=True),
        _observation("read_golden", "r1"),
    ]
    incomplete = [
        harness.ProviderRecallObservation(
            provider_id="tree-sitter",
            language="python",
            case_id="python",
            expected_tests=("tests/test_python.py",),
            routed_tests=("tests/test_python.py",),
        ),
        harness.ProviderRecallObservation(
            provider_id="tree-sitter",
            language="typescript",
            case_id="typescript",
            expected_tests=("tests/app.test.ts",),
            routed_tests=("tests/app.test.ts",),
        ),
    ]

    report = harness.evaluate_release_gates(
        corpus_observations,
        provider_recall_observations=incomplete,
    )

    assert report.passed is False
    assert report.provider_recall_passed is False


def test_fallback_recall_is_reported_without_blocking_passing_primary() -> None:
    harness = _benchmark()
    corpus_observations = [
        _observation("generated_changes", "g1"),
        _observation("patches", "p1"),
        _observation("seeded_bugs", "s1", regression_caught=True),
        _observation("read_golden", "r1"),
    ]
    provider_observations = [
        *(
            harness.ProviderRecallObservation(
                provider_id="tree-sitter",
                language=language,
                case_id=language,
                expected_tests=(f"tests/test_{language}.py",),
                routed_tests=(f"tests/test_{language}.py",),
            )
            for language in ("javascript", "python", "typescript")
        ),
        harness.ProviderRecallObservation(
            provider_id="syntax",
            language="javascript",
            case_id="fallback-javascript",
            expected_tests=("tests/loader.test.js",),
            routed_tests=(),
        ),
    ]

    report = harness.evaluate_release_gates(
        corpus_observations,
        provider_recall_observations=provider_observations,
    )

    assert report.passed is True
    assert report.provider_recall_passed is True
    assert any(
        metric.provider_id == "syntax" and metric.passed is False
        for metric in report.provider_recall
    )


def test_read_gate_requires_resume_metadata_for_every_truncation() -> None:
    harness = _benchmark()
    report = harness.evaluate_release_gates(
        [
            _observation(
                "read_golden",
                "missing-cursor",
                truncated=True,
                resume_metadata=False,
            )
        ]
    )

    metric = report.metric("read_golden")
    assert metric.success_count == 0
    assert metric.missing_resume_metadata_count == 1
    assert metric.passed is False


def test_missing_corpus_is_not_reported_as_green() -> None:
    harness = _benchmark()
    report = harness.evaluate_release_gates([])

    assert report.passed is False
    assert {metric.status for metric in report.metrics} == {"not_run"}


def test_corpus_loader_rejects_duplicate_ids_and_unknown_shapes(tmp_path: Path) -> None:
    harness = _benchmark()
    corpus = tmp_path / "generated_changes.json"
    corpus.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "corpus": "generated_changes",
                "cases": [
                    {"id": "duplicate", "input": {}, "expected": {}},
                    {"id": "duplicate", "input": {}, "expected": {}},
                ],
            }
        ),
        encoding="utf-8",
    )

    try:
        harness.load_corpus(corpus)
    except ValueError as exc:
        assert "duplicate case id" in str(exc)
    else:
        raise AssertionError("duplicate corpus ids must fail closed")


def test_report_publisher_writes_stable_json_and_markdown(tmp_path: Path) -> None:
    harness = _benchmark()
    report = harness.evaluate_release_gates(
        [
            _observation("generated_changes", "g1"),
            _observation("patches", "p1"),
            _observation("seeded_bugs", "s1", regression_caught=True),
            _observation("read_golden", "r1"),
        ],
        provider_recall_observations=[
            *(
                harness.ProviderRecallObservation(
                    provider_id="tree-sitter",
                    language=language,
                    case_id=language,
                    expected_tests=(f"tests/test_{language}.py",),
                    routed_tests=(f"tests/test_{language}.py",),
                )
                for language in ("javascript", "python", "typescript")
            ),
            harness.ProviderRecallObservation(
                provider_id="syntax",
                language="javascript",
                case_id="fallback-javascript",
                expected_tests=("tests/loader.test.js",),
                routed_tests=(),
            ),
        ],
    )

    paths = harness.publish_report(report, tmp_path)

    assert paths.json_path.name == "forge-v2-release-gates.json"
    assert paths.markdown_path.name == "forge-v2-release-gates.md"
    decoded = json.loads(paths.json_path.read_text(encoding="utf-8"))
    assert decoded["passed"] is True
    assert decoded["provider_recall_passed"] is True
    assert len(decoded["provider_recall"]) == 4
    markdown = paths.markdown_path.read_text(encoding="utf-8")
    assert "# Forge v2 Release Gates" in markdown
    assert "generated_changes" in markdown
    assert "Provider routed-test recall" in markdown
    assert "tree-sitter" in markdown
    assert "syntax" in markdown


def test_reference_executor_passes_every_frozen_v2_corpus() -> None:
    from repoforge.adapters.code_intelligence import TreeSitterCodeIntelligenceProvider
    from repoforge.benchmark.reference import ReferenceExecutor

    execute_case = ReferenceExecutor(TreeSitterCodeIntelligenceProvider())
    harness = _benchmark()
    report = harness.run_release_gates(
        execute_case,
        corpus_root=ROOT / "tests/fixtures/v2_corpora",
    )

    failures = [
        (case.case_id, execute_case(case).details)
        for case in harness.load_corpus(ROOT / "tests/fixtures/v2_corpora/patches.json")
        if not execute_case(case).success
    ]
    assert report.passed is True, failures
    assert all(metric.status == "passed" for metric in report.metrics)
    assert all(metric.wrong_target_count == 0 for metric in report.metrics)


def test_run_release_gates_executes_every_case_once(tmp_path: Path) -> None:
    harness = _benchmark()
    corpus_root = tmp_path / "corpora"
    corpus_root.mkdir()
    for name in harness.RELEASE_THRESHOLDS:
        (corpus_root / f"{name}.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "corpus": name,
                    "cases": [{"id": f"{name}-1", "input": {}, "expected": {}}],
                }
            ),
            encoding="utf-8",
        )

    seen: list[tuple[str, str]] = []

    def executor(case):
        seen.append((case.corpus, case.case_id))
        return _observation(
            case.corpus,
            case.case_id,
            regression_caught=case.corpus == "seeded_bugs",
        )

    report = harness.run_release_gates(executor, corpus_root=corpus_root)

    assert sorted(seen) == [
        ("generated_changes", "generated_changes-1"),
        ("patches", "patches-1"),
        ("read_golden", "read_golden-1"),
        ("seeded_bugs", "seeded_bugs-1"),
    ]
    assert report.passed is True


def _control_step(
    call: int,
    tool: str,
    kind: str,
    resource: str,
    outcome: str,
    *,
    state_version: str = "v1",
    cursor: str | None = None,
    effect_key: str | None = None,
    actionable_failure: bool = False,
    timestamp_state: str = "observed",
) -> dict[str, object]:
    return {
        "call": call,
        "tool": tool,
        "kind": kind,
        "resource": resource,
        "state_version": state_version,
        "cursor": cursor,
        "outcome": outcome,
        "effect_key": effect_key,
        "actionable_failure": actionable_failure,
        "temporary_mutation": False,
        "profile": None,
        "rerun": False,
        "timestamp_state": timestamp_state,
    }


def _control_identity():
    from repoforge.benchmark.control_plane import ControlPlaneIdentity

    return ControlPlaneIdentity(
        git_head="a" * 40,
        dirty=False,
        python_version="3.13.5",
        package_version="2.2.0",
        contract_version="forge_v2",
        tool_count=28,
        tool_surface_hash="b" * 64,
        schema_bundle_digest="c" * 64,
    )


def test_control_plane_gate_evaluates_recorded_traces_without_hidden_retries(
    tmp_path: Path,
) -> None:
    from repoforge.benchmark.control_plane import (
        CONTROL_PLANE_THRESHOLDS,
        ScenarioExecution,
        evaluate_control_plane_gates,
        load_control_plane_manifest,
    )

    manifest_path = tmp_path / "control-plane.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "thresholds": CONTROL_PLANE_THRESHOLDS,
                "scenarios": [
                    {
                        "id": "after-effect-reconcile",
                        "selector": "tests/test_example.py::test_after_effect",
                        "boundary": "after_effect",
                        "completed": True,
                        "trace": [
                            _control_step(
                                1,
                                "workspace_pr",
                                "mutate",
                                "pr:42",
                                "effect_outcome_unknown",
                                effect_key="comment-1",
                                actionable_failure=True,
                            ),
                            _control_step(
                                2,
                                "workspace_pr",
                                "control",
                                "pr:42",
                                "reconciled",
                                state_version="v2",
                                effect_key="comment-1",
                            ),
                        ],
                    },
                    {
                        "id": "bounded-read",
                        "selector": "tests/test_example.py::test_bounded_read",
                        "boundary": "bounded_retrieval",
                        "completed": True,
                        "trace": [
                            _control_step(
                                1,
                                "runtime_logs_read",
                                "read",
                                "failure-artifact:abc",
                                "partial",
                                state_version="sha256:abc",
                                cursor="start",
                                timestamp_state="unavailable",
                            ),
                            _control_step(
                                2,
                                "runtime_logs_read",
                                "read",
                                "failure-artifact:abc",
                                "resumed",
                                state_version="sha256:abc",
                                cursor="page:2",
                                timestamp_state="unavailable",
                            ),
                        ],
                    },
                ],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    manifest = load_control_plane_manifest(manifest_path)
    report = evaluate_control_plane_gates(
        manifest,
        (
            ScenarioExecution(
                scenario_id="after-effect-reconcile",
                selector="tests/test_example.py::test_after_effect",
                passed=True,
                duration_ms=10.0,
                attempts=1,
                output_excerpt="1 passed",
            ),
            ScenarioExecution(
                scenario_id="bounded-read",
                selector="tests/test_example.py::test_bounded_read",
                passed=True,
                duration_ms=5.0,
                attempts=1,
                output_excerpt="1 passed",
            ),
        ),
        identity=_control_identity(),
    )

    assert report.passed is True
    assert report.metrics.unknown_effect_outcomes == 0
    assert report.metrics.synthetic_timestamp_count == 0
    assert report.metrics.opaque_failure_count == 0
    assert report.metrics.calls_per_completed_task == 2.0
    assert report.metrics.duplicate_read_rate == 0.0
    assert report.metrics.max_actionable_failure_call == 1
    assert report.hidden_retry_count == 0


def test_control_plane_gate_fails_unreconciled_effect_and_hidden_retry(tmp_path: Path) -> None:
    from repoforge.benchmark.control_plane import (
        CONTROL_PLANE_THRESHOLDS,
        ScenarioExecution,
        evaluate_control_plane_gates,
        load_control_plane_manifest,
    )

    manifest_path = tmp_path / "control-plane-fail.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "thresholds": CONTROL_PLANE_THRESHOLDS,
                "scenarios": [
                    {
                        "id": "unresolved",
                        "selector": "tests/test_example.py::test_unresolved",
                        "boundary": "after_effect",
                        "completed": False,
                        "trace": [
                            _control_step(
                                1,
                                "repo_issue",
                                "mutate",
                                "issue:7",
                                "effect_outcome_unknown",
                                effect_key="comment-7",
                                actionable_failure=True,
                                timestamp_state="synthetic",
                            )
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    report = evaluate_control_plane_gates(
        load_control_plane_manifest(manifest_path),
        (
            ScenarioExecution(
                scenario_id="unresolved",
                selector="tests/test_example.py::test_unresolved",
                passed=True,
                duration_ms=1.0,
                attempts=2,
                output_excerpt="retried internally",
            ),
        ),
        identity=_control_identity(),
    )

    assert report.passed is False
    assert report.metrics.unknown_effect_outcomes == 1
    assert report.metrics.synthetic_timestamp_count == 1
    assert report.hidden_retry_count == 1


def test_committed_control_plane_fault_matrix_covers_epic_boundaries() -> None:
    from repoforge.benchmark.control_plane import load_control_plane_manifest

    manifest = load_control_plane_manifest(
        ROOT / "tests/fixtures/v2_corpora/control_plane_faults.json"
    )

    assert {scenario.boundary for scenario in manifest.scenarios} >= {
        "before_effect",
        "commit",
        "after_effect",
        "serialization",
        "result_persistence",
        "caller_disconnect",
        "stale_identity",
        "logs",
        "bounded_retrieval",
        "pr_drift",
        "graph_projection",
        "generated_refresh",
    }
    assert len({scenario.selector for scenario in manifest.scenarios}) == len(manifest.scenarios)


def test_control_plane_runner_executes_each_scenario_once(tmp_path: Path) -> None:
    from repoforge.benchmark.control_plane import (
        CONTROL_PLANE_THRESHOLDS,
        ScenarioExecution,
        load_control_plane_manifest,
        run_control_plane_scenarios,
    )

    manifest_path = tmp_path / "control-plane-runner.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "thresholds": CONTROL_PLANE_THRESHOLDS,
                "scenarios": [
                    {
                        "id": "one",
                        "selector": "tests/test_example.py::test_one",
                        "boundary": "before_effect",
                        "completed": True,
                        "trace": [
                            _control_step(
                                1,
                                "workspace_mutate",
                                "mutate",
                                "workspace:one",
                                "failed_before_effect",
                                actionable_failure=True,
                            )
                        ],
                    },
                    {
                        "id": "two",
                        "selector": "tests/test_example.py::test_two",
                        "boundary": "logs",
                        "completed": True,
                        "trace": [
                            _control_step(
                                1,
                                "runtime_logs_read",
                                "read",
                                "runtime:two",
                                "completed",
                            )
                        ],
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    manifest = load_control_plane_manifest(manifest_path)
    seen: list[str] = []

    def executor(scenario):
        seen.append(scenario.scenario_id)
        return ScenarioExecution(
            scenario_id=scenario.scenario_id,
            selector=scenario.selector,
            passed=True,
            duration_ms=1.0,
            attempts=1,
            output_excerpt="1 passed",
        )

    executions = run_control_plane_scenarios(manifest, executor)

    assert seen == ["one", "two"]
    assert [item.scenario_id for item in executions] == ["one", "two"]


def test_control_plane_report_publishes_exact_identity_and_metrics(tmp_path: Path) -> None:
    from repoforge.benchmark.control_plane import (
        CONTROL_PLANE_THRESHOLDS,
        ScenarioExecution,
        evaluate_control_plane_gates,
        load_control_plane_manifest,
        publish_control_plane_report,
    )

    manifest_path = tmp_path / "control-plane-report.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "thresholds": CONTROL_PLANE_THRESHOLDS,
                "scenarios": [
                    {
                        "id": "report",
                        "selector": "tests/test_example.py::test_report",
                        "boundary": "logs",
                        "completed": True,
                        "trace": [
                            _control_step(
                                1,
                                "runtime_logs_read",
                                "read",
                                "runtime:report",
                                "completed",
                            )
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    secret = "ghp_" + "x" * 40
    report = evaluate_control_plane_gates(
        load_control_plane_manifest(manifest_path),
        (
            ScenarioExecution(
                scenario_id="report",
                selector="tests/test_example.py::test_report",
                passed=True,
                duration_ms=1.0,
                attempts=1,
                output_excerpt=f"failure token={secret}",
            ),
        ),
        identity=_control_identity(),
    )

    paths = publish_control_plane_report(report, tmp_path / "reports")

    raw_json = paths.json_path.read_text(encoding="utf-8")
    payload = json.loads(raw_json)
    assert payload["identity"]["git_head"] == "a" * 40
    assert payload["identity"]["tool_count"] == 28
    assert payload["identity"]["tool_surface_hash"] == "b" * 64
    assert payload["metrics"]["unknown_effect_outcomes"] == 0
    assert secret not in raw_json
    assert "<redacted" in raw_json
    markdown = paths.markdown_path.read_text(encoding="utf-8")
    assert secret not in markdown
    assert "Control-plane fault gates" in markdown
    assert "Calls per completed task" in markdown


def test_make_v2_gates_runs_control_plane_matrix_after_reference_corpora() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")

    reference = "scripts/run_v2_release_gates.py"
    control_plane = "scripts/run_control_plane_gates.py"
    assert reference in makefile
    assert control_plane in makefile
    assert makefile.index(reference) < makefile.index(control_plane)
    assert "tests/fixtures/v2_corpora/control_plane_faults.json" in makefile
