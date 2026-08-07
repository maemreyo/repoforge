"""Shared eligibility policy for semantic-graph verification routing."""

from __future__ import annotations

from collections.abc import Mapping

from .code_intelligence_model import CodeIntelligenceStatus, SemanticGraphEvidence

TARGETED_VERIFICATION_CONFIDENCE = 95


def semantic_graph_allows_targeted_routing(
    graph: SemanticGraphEvidence | None,
) -> bool:
    return bool(
        graph is not None
        and graph.status is CodeIntelligenceStatus.CURRENT
        and graph.coverage.value == 100
        and graph.confidence.value >= TARGETED_VERIFICATION_CONFIDENCE
        and not graph.truncated
    )


def semantic_graph_widening_reason(
    graph: SemanticGraphEvidence | None,
) -> str | None:
    if graph is None or semantic_graph_allows_targeted_routing(graph):
        return None
    if graph.status is CodeIntelligenceStatus.UNAVAILABLE:
        state = "unavailable"
    elif graph.status is CodeIntelligenceStatus.PARTIAL:
        state = "partial"
    elif graph.truncated:
        state = "truncated"
    elif graph.coverage.value < 100:
        state = "incomplete coverage"
    else:
        state = "below the targeted-routing confidence threshold"
    return (
        f"Semantic graph evidence is {state}; targeted verification must retain the final profile."
    )


def _measure_value(raw: object) -> int | None:
    if not isinstance(raw, Mapping):
        return None
    value = raw.get("value")
    if not isinstance(value, int) or isinstance(value, bool):
        return None
    return value


def semantic_graph_payload_allows_targeted_routing(payload: object) -> bool:
    if not isinstance(payload, Mapping):
        return False
    return bool(
        payload.get("status") == CodeIntelligenceStatus.CURRENT.value
        and _measure_value(payload.get("coverage")) == 100
        and (_measure_value(payload.get("confidence")) or -1) >= TARGETED_VERIFICATION_CONFIDENCE
        and payload.get("truncated") is False
    )


__all__ = [
    "TARGETED_VERIFICATION_CONFIDENCE",
    "semantic_graph_allows_targeted_routing",
    "semantic_graph_payload_allows_targeted_routing",
    "semantic_graph_widening_reason",
]
