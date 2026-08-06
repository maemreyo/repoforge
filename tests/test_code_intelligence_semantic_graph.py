from __future__ import annotations

from dataclasses import dataclass

from repoforge.application.dto import to_data
from repoforge.domain.code_intelligence import (
    AffectedPathCandidate,
    CodeIntelligenceMeasure,
    CodeIntelligenceResult,
    CodeIntelligenceSnapshot,
    CodeIntelligenceStatus,
    CodeRelationshipFact,
    CodeRelationshipKind,
    SemanticGraphEvidence,
    new_code_intelligence_result,
)


def _snapshot() -> CodeIntelligenceSnapshot:
    return CodeIntelligenceSnapshot(
        repo_id="demo",
        workspace_id="workspace-1",
        head_sha="a" * 40,
        workspace_fingerprint="b" * 64,
    )


@dataclass(frozen=True, slots=True)
class _LegacyDto:
    optional_value: str | None = None


def test_unmarked_dataclass_none_keeps_existing_serialization() -> None:
    assert to_data(_LegacyDto()) == {"optional_value": None}


def _baseline_result(
    *, semantic_graph: SemanticGraphEvidence | None = None
) -> CodeIntelligenceResult:
    return new_code_intelligence_result(
        provider_id="tree-sitter",
        provider_version="1",
        snapshot=_snapshot(),
        status=CodeIntelligenceStatus.CURRENT,
        coverage=CodeIntelligenceMeasure(100, "All supported paths were analyzed."),
        confidence=CodeIntelligenceMeasure(100, "Baseline calibration matched."),
        analyzed_paths=("src/service.py",),
        semantic_graph=semantic_graph,
    )


def test_disabled_baseline_serialization_omits_semantic_graph() -> None:
    result = _baseline_result()

    assert result.semantic_graph is None
    assert "semantic_graph" not in to_data(result)


def test_semantic_graph_contract_normalizes_and_serializes_facts() -> None:
    first = CodeRelationshipFact(
        kind=CodeRelationshipKind.CALLS,
        source_path="src/a.py",
        source_symbol="a.run",
        target_path="src/b.py",
        target_symbol="b.run",
        depth=1,
        confidence=95,
    )
    second = CodeRelationshipFact(
        kind=CodeRelationshipKind.CALLS,
        source_path="src/b.py",
        source_symbol="b.run",
        target_path="src/a.py",
        target_symbol="a.run",
        depth=2,
        confidence=90,
    )
    graph = SemanticGraphEvidence(
        provider_id="codegraph",
        provider_version="1.5.0",
        status=CodeIntelligenceStatus.CURRENT,
        coverage=CodeIntelligenceMeasure(100, "Indexed requested paths."),
        confidence=CodeIntelligenceMeasure(100, "Semantic canary receipt matched."),
        relationships=(second, first, second),
        affected_paths=(
            AffectedPathCandidate("tests/test_b.py", "impact depth 2", 90, 2),
            AffectedPathCandidate("tests/test_a.py", "impact depth 1", 95, 1),
        ),
    )

    result = _baseline_result(semantic_graph=graph)
    payload = to_data(result)

    assert result.semantic_graph is graph
    assert graph.relationships == (first, second)
    assert [item.path for item in graph.affected_paths] == [
        "tests/test_a.py",
        "tests/test_b.py",
    ]
    assert payload["semantic_graph"]["status"] == "current"
    assert payload["semantic_graph"]["relationships"][0]["source_symbol"] == "a.run"
