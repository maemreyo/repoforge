"""Deterministic corpus execution, threshold evaluation, and report publication."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

CorpusName = Literal["generated_changes", "patches", "seeded_bugs", "read_golden"]
GateStatus = Literal["passed", "failed", "not_run"]

RELEASE_THRESHOLDS: dict[CorpusName, float] = {
    "generated_changes": 0.99,
    "patches": 0.95,
    "seeded_bugs": 1.0,
    "read_golden": 1.0,
}
_CORPUS_ORDER: tuple[CorpusName, ...] = tuple(RELEASE_THRESHOLDS)


@dataclass(frozen=True, slots=True)
class CorpusCase:
    corpus: CorpusName
    case_id: str
    input: Mapping[str, object]
    expected: Mapping[str, object]
    metadata: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class CaseObservation:
    corpus: CorpusName
    case_id: str
    success: bool
    wrong_target: bool
    regression_caught: bool
    fell_back_full: bool
    truncated: bool
    resume_metadata: bool
    duration_ms: float
    details: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class GateMetric:
    corpus: CorpusName
    threshold: float
    total_count: int
    success_count: int
    success_rate: float
    wrong_target_count: int
    missing_resume_metadata_count: int
    duration_ms: float
    status: GateStatus

    @property
    def passed(self) -> bool:
        return self.status == "passed"


@dataclass(frozen=True, slots=True)
class ReleaseGateReport:
    schema_version: int
    metrics: tuple[GateMetric, ...]
    passed: bool

    def metric(self, corpus: CorpusName) -> GateMetric:
        for metric in self.metrics:
            if metric.corpus == corpus:
                return metric
        raise KeyError(corpus)

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "passed": self.passed,
            "metrics": [asdict(metric) for metric in self.metrics],
        }


@dataclass(frozen=True, slots=True)
class ReportPaths:
    json_path: Path
    markdown_path: Path


CaseExecutor = Callable[[CorpusCase], CaseObservation]


def _expect_mapping(value: object, context: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValueError(f"{context} must be a JSON object")
    return dict(value)


def _corpus_name(value: object, context: str) -> CorpusName:
    if not isinstance(value, str) or value not in RELEASE_THRESHOLDS:
        raise ValueError(f"{context} must be one of {list(RELEASE_THRESHOLDS)}")
    return value


def load_corpus(path: Path) -> tuple[CorpusCase, ...]:
    """Load one fail-closed, deterministic corpus file."""

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot load corpus {path.name}: {exc}") from exc
    document = _expect_mapping(raw, path.name)
    if set(document) != {"schema_version", "corpus", "cases"}:
        raise ValueError(f"{path.name} must contain exactly schema_version, corpus, and cases")
    if document["schema_version"] != 1:
        raise ValueError(f"{path.name} uses an unsupported schema_version")
    corpus = _corpus_name(document["corpus"], f"{path.name}.corpus")
    raw_cases = document["cases"]
    if not isinstance(raw_cases, list) or not raw_cases:
        raise ValueError(f"{path.name}.cases must be a non-empty array")

    seen: set[str] = set()
    cases: list[CorpusCase] = []
    for index, raw_case in enumerate(raw_cases):
        case = _expect_mapping(raw_case, f"{path.name}.cases[{index}]")
        if set(case) - {"id", "input", "expected", "metadata"}:
            raise ValueError(f"{path.name}.cases[{index}] contains unsupported fields")
        case_id = case.get("id")
        if not isinstance(case_id, str) or not case_id or len(case_id) > 160:
            raise ValueError(f"{path.name}.cases[{index}].id must be 1-160 characters")
        if case_id in seen:
            raise ValueError(f"{path.name}: duplicate case id {case_id!r}")
        seen.add(case_id)
        cases.append(
            CorpusCase(
                corpus=corpus,
                case_id=case_id,
                input=_expect_mapping(case.get("input", {}), f"{case_id}.input"),
                expected=_expect_mapping(case.get("expected", {}), f"{case_id}.expected"),
                metadata=_expect_mapping(case.get("metadata", {}), f"{case_id}.metadata"),
            )
        )
    return tuple(cases)


def _case_passed(observation: CaseObservation) -> bool:
    if observation.corpus == "seeded_bugs":
        return observation.regression_caught or observation.fell_back_full
    if observation.corpus == "read_golden":
        return observation.success and (not observation.truncated or observation.resume_metadata)
    return observation.success


def _threshold_passed(corpus: CorpusName, success_rate: float) -> bool:
    threshold = RELEASE_THRESHOLDS[corpus]
    if corpus in {"generated_changes", "patches"}:
        return success_rate > threshold
    return success_rate >= threshold


def evaluate_release_gates(
    observations: Iterable[CaseObservation],
) -> ReleaseGateReport:
    """Evaluate all four blocking release gates without treating missing data as green."""

    grouped: dict[CorpusName, list[CaseObservation]] = {corpus: [] for corpus in _CORPUS_ORDER}
    seen: set[tuple[CorpusName, str]] = set()
    for observation in observations:
        corpus = _corpus_name(observation.corpus, "observation.corpus")
        identity = (corpus, observation.case_id)
        if identity in seen:
            raise ValueError(f"duplicate observation for {corpus}/{observation.case_id}")
        seen.add(identity)
        if observation.duration_ms < 0:
            raise ValueError("observation duration_ms cannot be negative")
        grouped[corpus].append(observation)

    metrics: list[GateMetric] = []
    for corpus in _CORPUS_ORDER:
        items = grouped[corpus]
        total = len(items)
        successes = sum(_case_passed(item) for item in items)
        wrong_targets = sum(item.wrong_target for item in items)
        missing_resume = sum(
            item.corpus == "read_golden" and item.truncated and not item.resume_metadata
            for item in items
        )
        rate = successes / total if total else 0.0
        if not total:
            status: GateStatus = "not_run"
        elif _threshold_passed(corpus, rate) and wrong_targets == 0 and missing_resume == 0:
            status = "passed"
        else:
            status = "failed"
        metrics.append(
            GateMetric(
                corpus=corpus,
                threshold=RELEASE_THRESHOLDS[corpus],
                total_count=total,
                success_count=successes,
                success_rate=rate,
                wrong_target_count=wrong_targets,
                missing_resume_metadata_count=missing_resume,
                duration_ms=round(sum(item.duration_ms for item in items), 3),
                status=status,
            )
        )
    frozen_metrics = tuple(metrics)
    return ReleaseGateReport(
        schema_version=1,
        metrics=frozen_metrics,
        passed=all(metric.passed for metric in frozen_metrics),
    )


def run_release_gates(
    executor: CaseExecutor,
    *,
    corpus_root: Path,
) -> ReleaseGateReport:
    """Execute every committed corpus case exactly once and evaluate the result."""

    observations: list[CaseObservation] = []
    for corpus in _CORPUS_ORDER:
        path = corpus_root / f"{corpus}.json"
        cases = load_corpus(path)
        if any(case.corpus != corpus for case in cases):
            raise ValueError(f"{path.name} declares the wrong corpus")
        for case in cases:
            observation = executor(case)
            if observation.corpus != case.corpus or observation.case_id != case.case_id:
                raise ValueError("Executor returned an observation for the wrong corpus or case id")
            observations.append(observation)
    return evaluate_release_gates(observations)


def _markdown(report: ReleaseGateReport) -> str:
    lines = [
        "# Forge v2 Release Gates",
        "",
        f"Overall: **{'PASS' if report.passed else 'FAIL'}**",
        "",
        "| Corpus | Status | Success | Threshold | Wrong target | Missing resume | Duration ms |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for metric in report.metrics:
        comparator = ">" if metric.corpus in {"generated_changes", "patches"} else ">="
        lines.append(
            "| "
            f"{metric.corpus} | {metric.status} | "
            f"{metric.success_count}/{metric.total_count} ({metric.success_rate:.3%}) | "
            f"{comparator} {metric.threshold:.1%} | {metric.wrong_target_count} | "
            f"{metric.missing_resume_metadata_count} | {metric.duration_ms:.3f} |"
        )
    return "\n".join(lines) + "\n"


def publish_report(report: ReleaseGateReport, output_dir: Path) -> ReportPaths:
    """Publish byte-stable machine and human reports."""

    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "forge-v2-release-gates.json"
    markdown_path = output_dir / "forge-v2-release-gates.md"
    json_path.write_text(
        json.dumps(report.as_dict(), indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    markdown_path.write_text(_markdown(report), encoding="utf-8")
    return ReportPaths(json_path=json_path, markdown_path=markdown_path)


def observations_from_json(path: Path) -> tuple[CaseObservation, ...]:
    """Load executor observations for offline report reproduction."""

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot load observations: {exc}") from exc
    if not isinstance(raw, list):
        raise ValueError("Observations must be a JSON array")
    observations: list[CaseObservation] = []
    for index, item in enumerate(raw):
        payload = _expect_mapping(item, f"observations[{index}]")
        try:
            corpus = _corpus_name(payload.pop("corpus"), f"observations[{index}].corpus")
            details = _expect_mapping(payload.pop("details", {}), f"observations[{index}].details")
            observations.append(CaseObservation(corpus=corpus, details=details, **payload))  # type: ignore[arg-type]
        except (KeyError, TypeError) as exc:
            raise ValueError(f"Invalid observation at index {index}: {exc}") from exc
    return tuple(observations)
