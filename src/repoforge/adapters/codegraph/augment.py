"""Baseline-preserving CodeGraph augmentation and per-repository routing."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import PurePosixPath
from typing import Protocol

from ...domain.code_intelligence import (
    MAX_CODE_INTELLIGENCE_FACTS,
    MAX_CODE_INTELLIGENCE_LIMITATIONS,
    AffectedTestCandidate,
    CodeIntelligenceMeasure,
    CodeIntelligenceRequest,
    CodeIntelligenceResult,
    CodeIntelligenceStatus,
    SemanticGraphEvidence,
    new_code_intelligence_result,
)
from ...domain.code_intelligence_routing import (
    semantic_graph_allows_targeted_routing,
    semantic_graph_widening_reason,
)
from ...ports.code_intelligence import CodeIntelligenceProvider


class SemanticGraphProvider(Protocol):
    @property
    def provider_id(self) -> str: ...

    @property
    def provider_version(self) -> str: ...

    def analyze(
        self,
        request: CodeIntelligenceRequest,
        baseline: CodeIntelligenceResult,
    ) -> SemanticGraphEvidence: ...


def _is_python_test(path: str) -> bool:
    pure = PurePosixPath(path)
    if pure.suffix.lower() not in {".py", ".pyi"}:
        return False
    name = pure.name.lower()
    parts = {part.lower() for part in pure.parts}
    return "tests" in parts or name.startswith("test_") or name.endswith("_test.py")


def _mapped_candidates(
    request: CodeIntelligenceRequest,
    graph: SemanticGraphEvidence,
) -> tuple[AffectedTestCandidate, ...]:
    if "pytest-target" not in request.diagnostic_ids:
        return ()
    allowed = frozenset(request.paths) - frozenset(request.denied_paths)
    candidates: list[AffectedTestCandidate] = []
    for item in graph.affected_paths:
        if item.path not in allowed or not _is_python_test(item.path):
            continue
        candidates.append(
            AffectedTestCandidate(
                test_path=item.path,
                reason=item.reason,
                confidence=min(item.confidence, graph.confidence.value),
                diagnostic_id="pytest-target",
                selector=item.path,
            )
        )
    return tuple(candidates)


def _unavailable_graph(
    provider_id: str,
    provider_version: str,
    limitation: str,
) -> SemanticGraphEvidence:
    reason = "Managed semantic graph augmentation is unavailable."
    return SemanticGraphEvidence(
        provider_id=provider_id,
        provider_version=provider_version,
        status=CodeIntelligenceStatus.UNAVAILABLE,
        coverage=CodeIntelligenceMeasure(0, reason),
        confidence=CodeIntelligenceMeasure(0, reason),
        limitations=(limitation,),
    )


class UnavailableSemanticGraphProvider:
    """Return typed unavailable evidence for a configured enrollment that cannot run."""

    def __init__(self, provider_id: str, provider_version: str, limitation: str) -> None:
        self._provider_id = provider_id
        self._provider_version = provider_version
        self._limitation = limitation

    @property
    def provider_id(self) -> str:
        return self._provider_id

    @property
    def provider_version(self) -> str:
        return self._provider_version

    def analyze(
        self,
        request: CodeIntelligenceRequest,
        baseline: CodeIntelligenceResult,
    ) -> SemanticGraphEvidence:
        del request, baseline
        return _unavailable_graph(
            self.provider_id,
            self.provider_version,
            self._limitation,
        )


class CodeGraphAugmentedProvider:
    """Attach graph evidence without weakening or replacing baseline facts."""

    def __init__(
        self,
        base: CodeIntelligenceProvider,
        graph: SemanticGraphProvider | None,
    ) -> None:
        self._base = base
        self._graph = graph

    @property
    def provider_id(self) -> str:
        return self._base.provider_id

    @property
    def provider_version(self) -> str:
        return self._base.provider_version

    def analyze(self, request: CodeIntelligenceRequest) -> CodeIntelligenceResult:
        baseline = self._base.analyze(request)
        if self._graph is None:
            return baseline
        graph_provider = self._graph
        try:
            graph = graph_provider.analyze(request, baseline)
            if not isinstance(graph, SemanticGraphEvidence):
                raise TypeError("semantic graph provider returned an invalid result type")
        except Exception as exc:
            graph = _unavailable_graph(
                graph_provider.provider_id,
                graph_provider.provider_version,
                "Managed semantic graph augmentation stopped at a reviewed boundary "
                f"({type(exc).__name__}).",
            )

        candidates = (*baseline.affected_tests, *_mapped_candidates(request, graph))
        if len(candidates) > MAX_CODE_INTELLIGENCE_FACTS:
            candidates = candidates[:MAX_CODE_INTELLIGENCE_FACTS]
        limitations = list((*baseline.limitations, *graph.limitations))
        widening = semantic_graph_widening_reason(graph)
        if widening is not None and widening not in limitations:
            limitations.append(widening)
        limitations = limitations[:MAX_CODE_INTELLIGENCE_LIMITATIONS]
        return new_code_intelligence_result(
            provider_id=baseline.provider_id,
            provider_version=baseline.provider_version,
            snapshot=baseline.snapshot,
            status=baseline.status,
            coverage=baseline.coverage,
            confidence=baseline.confidence,
            analyzed_paths=baseline.analyzed_paths,
            symbols=baseline.symbols,
            imports=baseline.imports,
            references=baseline.references,
            affected_tests=tuple(candidates),
            unsupported_paths=baseline.unsupported_paths,
            malformed_paths=baseline.malformed_paths,
            generated_paths=baseline.generated_paths,
            denied_paths=baseline.denied_paths,
            limitations=tuple(limitations),
            truncated=baseline.truncated,
            semantic_graph=graph,
        )


class RepositoryCodeIntelligenceRouter:
    """Select reviewed augmentation per repository while keeping baseline default."""

    def __init__(
        self,
        *,
        default_provider: CodeIntelligenceProvider,
        providers: Mapping[str, CodeIntelligenceProvider],
    ) -> None:
        self._default = default_provider
        self._providers = dict(providers)

    @property
    def provider_id(self) -> str:
        return "repository-router"

    @property
    def provider_version(self) -> str:
        return "1"

    def analyze(self, request: CodeIntelligenceRequest) -> CodeIntelligenceResult:
        provider = self._providers.get(request.snapshot.repo_id, self._default)
        return provider.analyze(request)


__all__ = [
    "CodeGraphAugmentedProvider",
    "RepositoryCodeIntelligenceRouter",
    "SemanticGraphProvider",
    "UnavailableSemanticGraphProvider",
    "semantic_graph_allows_targeted_routing",
    "semantic_graph_widening_reason",
]
