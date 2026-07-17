from __future__ import annotations

import json
from pathlib import Path


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
        ]
    )

    paths = harness.publish_report(report, tmp_path)

    assert paths.json_path.name == "forge-v2-release-gates.json"
    assert paths.markdown_path.name == "forge-v2-release-gates.md"
    decoded = json.loads(paths.json_path.read_text(encoding="utf-8"))
    assert decoded["passed"] is True
    markdown = paths.markdown_path.read_text(encoding="utf-8")
    assert "# Forge v2 Release Gates" in markdown
    assert "generated_changes" in markdown


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
