#!/usr/bin/env python3
"""Evaluate or execute the blocking Forge v2 release-gate corpora."""

from __future__ import annotations

import argparse
import importlib
import sys
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from repoforge.adapters.code_intelligence import (  # noqa: E402
    SyntaxCodeIntelligenceProvider,
    TreeSitterCodeIntelligenceProvider,
)
from repoforge.benchmark.code_intelligence import measure_provider_recall  # noqa: E402
from repoforge.benchmark.harness import (  # noqa: E402
    CaseExecutor,
    CorpusCase,
    ProviderRecallObservation,
    evaluate_release_gates,
    load_corpus,
    observations_from_json,
    publish_report,
    run_release_gates,
)

DEFAULT_CORPUS_ROOT = ROOT / "tests" / "fixtures" / "v2_corpora"
DEFAULT_REPORT_DIR = ROOT / "build" / "reports"


def _load_executor(reference: str) -> CaseExecutor:
    module_name, separator, attribute = reference.partition(":")
    if not separator or not module_name or not attribute:
        raise ValueError("--executor must use module:function syntax")
    module = importlib.import_module(module_name)
    candidate = getattr(module, attribute, None)
    if not callable(candidate):
        raise ValueError(f"Executor is not callable: {reference}")
    executor: Callable[[CorpusCase], object] = candidate

    def checked(case: CorpusCase):
        result = executor(case)
        from repoforge.benchmark.harness import CaseObservation

        if not isinstance(result, CaseObservation):
            raise TypeError("Release-gate executor must return CaseObservation")
        return result

    return checked


def _provider_recall_observations(
    corpus_root: Path,
) -> tuple[ProviderRecallObservation, ...]:
    cases = load_corpus(corpus_root / "seeded_bugs.json")
    return (
        *measure_provider_recall(TreeSitterCodeIntelligenceProvider(), cases),
        *measure_provider_recall(SyntaxCodeIntelligenceProvider(), cases),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--executor",
        help="Execute corpora with a reviewed module:function CaseExecutor.",
    )
    mode.add_argument(
        "--observations",
        type=Path,
        help="Reproduce a report from previously captured observation JSON.",
    )
    parser.add_argument("--corpus-root", type=Path, default=DEFAULT_CORPUS_ROOT)
    parser.add_argument("--report-dir", type=Path, default=DEFAULT_REPORT_DIR)
    args = parser.parse_args()

    try:
        provider_recall = _provider_recall_observations(args.corpus_root)
        if args.executor:
            report = run_release_gates(
                _load_executor(args.executor),
                corpus_root=args.corpus_root,
                provider_recall_observations=provider_recall,
            )
        else:
            report = evaluate_release_gates(
                observations_from_json(args.observations),
                provider_recall_observations=provider_recall,
            )
        paths = publish_report(report, args.report_dir)
    except (ImportError, AttributeError, OSError, TypeError, ValueError) as exc:
        print(f"Forge v2 release gates could not run: {exc}", file=sys.stderr)
        return 2

    print(f"report: {paths.json_path}")
    print(f"summary: {paths.markdown_path}")
    for metric in report.metrics:
        print(
            f"{metric.corpus}: {metric.status} "
            f"({metric.success_count}/{metric.total_count}, "
            f"wrong_target={metric.wrong_target_count})"
        )
    for metric in report.provider_recall:
        print(
            f"provider-recall {metric.provider_id}/{metric.language}: "
            f"{metric.matched_test_count}/{metric.expected_test_count} "
            f"({metric.recall:.1%})"
        )
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
