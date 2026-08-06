from __future__ import annotations

from pathlib import Path

from codegraph_provider_support import manifest

from repoforge.adapters.codegraph.augment import (
    CodeGraphAugmentedProvider,
    RepositoryCodeIntelligenceRouter,
)
from repoforge.adapters.codegraph.composition import build_repository_code_intelligence
from repoforge.bootstrap import AdapterOverrides, build_application
from repoforge.config import AppConfig, RepositoryConfig, ServerConfig
from repoforge.domain.code_intelligence import (
    AffectedPathCandidate,
    AffectedTestCandidate,
    CodeIntelligenceMeasure,
    CodeIntelligenceRequest,
    CodeIntelligenceSnapshot,
    CodeIntelligenceStatus,
    SemanticGraphEvidence,
    new_code_intelligence_result,
)


def _request(tmp_path: Path, repo_id: str = "demo") -> CodeIntelligenceRequest:
    return CodeIntelligenceRequest(
        workspace_root=tmp_path,
        snapshot=CodeIntelligenceSnapshot(repo_id, "workspace-1", "a" * 40, "b" * 64),
        paths=("src/service.py", "tests/test_service.py", "web/service.test.ts"),
        changed_paths=("src/service.py",),
        diagnostic_ids=("pytest-target",),
    )


def _baseline(request: CodeIntelligenceRequest):
    return new_code_intelligence_result(
        provider_id="tree-sitter",
        provider_version="1",
        snapshot=request.snapshot,
        status=CodeIntelligenceStatus.CURRENT,
        coverage=CodeIntelligenceMeasure(100, "Baseline coverage."),
        confidence=CodeIntelligenceMeasure(92, "Baseline confidence."),
        analyzed_paths=request.paths,
        affected_tests=(
            AffectedTestCandidate(
                "tests/test_service.py",
                "Baseline import traversal.",
                80,
                "pytest-target",
                "tests/test_service.py",
            ),
        ),
        limitations=("Baseline does not model runtime dispatch.",),
    )


class BaseProvider:
    provider_id = "tree-sitter"
    provider_version = "1"

    def __init__(self, result) -> None:
        self.result = result
        self.calls = 0

    def analyze(self, request: CodeIntelligenceRequest):
        assert request.snapshot == self.result.snapshot
        self.calls += 1
        return self.result


class GraphProvider:
    provider_id = "codegraph"
    provider_version = "1.5.0"

    def __init__(self, evidence: SemanticGraphEvidence | Exception) -> None:
        self.evidence = evidence
        self.calls = 0

    def analyze(self, request: CodeIntelligenceRequest, baseline):
        assert request.snapshot == baseline.snapshot
        self.calls += 1
        if isinstance(self.evidence, Exception):
            raise self.evidence
        return self.evidence


def _graph(
    *,
    status: CodeIntelligenceStatus = CodeIntelligenceStatus.CURRENT,
    confidence: int = 90,
    truncated: bool = False,
    limitations: tuple[str, ...] = (),
) -> SemanticGraphEvidence:
    actual_limitations = limitations
    if status is not CodeIntelligenceStatus.CURRENT and not actual_limitations:
        actual_limitations = ("Graph evidence is incomplete.",)
    affected = (
        ()
        if status is CodeIntelligenceStatus.UNAVAILABLE
        else (
            AffectedPathCandidate(
                "tests/test_service.py",
                "Semantic impact traversal.",
                confidence,
                2,
            ),
            AffectedPathCandidate(
                "web/service.test.ts",
                "Semantic impact traversal.",
                confidence,
                2,
            ),
        )
    )
    return SemanticGraphEvidence(
        "codegraph",
        "1.5.0",
        status,
        CodeIntelligenceMeasure(
            100 if status is not CodeIntelligenceStatus.UNAVAILABLE else 0, "Graph coverage."
        ),
        CodeIntelligenceMeasure(confidence, "Graph confidence."),
        affected_paths=affected,
        limitations=actual_limitations,
        truncated=truncated,
    )


def test_disabled_augmentation_returns_exact_baseline_object(tmp_path: Path) -> None:
    request = _request(tmp_path)
    baseline = _baseline(request)
    base = BaseProvider(baseline)
    provider = CodeGraphAugmentedProvider(base, None)

    result = provider.analyze(request)

    assert result is baseline
    assert base.calls == 1


def test_current_graph_adds_only_reviewed_diagnostic_candidates(tmp_path: Path) -> None:
    request = _request(tmp_path)
    baseline = _baseline(request)
    provider = CodeGraphAugmentedProvider(BaseProvider(baseline), GraphProvider(_graph()))

    result = provider.analyze(request)

    assert result.status is baseline.status
    assert result.coverage is baseline.coverage
    assert result.confidence is baseline.confidence
    assert result.symbols == baseline.symbols
    assert result.imports == baseline.imports
    assert result.references == baseline.references
    assert result.semantic_graph is not None
    assert {item.test_path for item in result.affected_tests} == {"tests/test_service.py"}
    added = next(
        item for item in result.affected_tests if item.reason == "Semantic impact traversal."
    )
    assert added.diagnostic_id == "pytest-target"
    assert added.selector == "tests/test_service.py"
    assert all(item.test_path != "web/service.test.ts" for item in result.affected_tests)


def test_partial_graph_preserves_baseline_and_records_widening_reason(tmp_path: Path) -> None:
    request = _request(tmp_path)
    baseline = _baseline(request)
    graph = _graph(status=CodeIntelligenceStatus.PARTIAL, truncated=True)
    provider = CodeGraphAugmentedProvider(BaseProvider(baseline), GraphProvider(graph))

    result = provider.analyze(request)

    assert result.status is CodeIntelligenceStatus.CURRENT
    assert result.truncated is baseline.truncated
    assert result.analyzed_paths == baseline.analyzed_paths
    assert result.affected_tests[:1] == baseline.affected_tests
    assert result.semantic_graph is graph
    assert any("final profile" in limitation.lower() for limitation in result.limitations)


def test_unavailable_graph_never_removes_baseline_facts(tmp_path: Path) -> None:
    request = _request(tmp_path)
    baseline = _baseline(request)
    graph = _graph(status=CodeIntelligenceStatus.UNAVAILABLE, confidence=0)
    provider = CodeGraphAugmentedProvider(BaseProvider(baseline), GraphProvider(graph))

    result = provider.analyze(request)

    assert result.affected_tests == baseline.affected_tests
    assert result.semantic_graph is graph
    assert result.provider_id == baseline.provider_id
    assert result.provider_version == baseline.provider_version


def test_graph_exception_is_sanitized_and_baseline_survives(tmp_path: Path) -> None:
    request = _request(tmp_path)
    baseline = _baseline(request)
    provider = CodeGraphAugmentedProvider(
        BaseProvider(baseline),
        GraphProvider(RuntimeError("provider-secret")),
    )

    result = provider.analyze(request)

    assert result.affected_tests == baseline.affected_tests
    assert result.semantic_graph is not None
    assert result.semantic_graph.status is CodeIntelligenceStatus.UNAVAILABLE
    assert "provider-secret" not in repr(result)
    assert any("runtimeerror" in limitation.lower() for limitation in result.limitations)


def test_repository_router_selects_augmented_provider_per_repo(tmp_path: Path) -> None:
    enabled_request = _request(tmp_path, "enabled")
    disabled_request = _request(tmp_path, "disabled")
    enabled_baseline = _baseline(enabled_request)
    disabled_baseline = _baseline(disabled_request)
    enabled_base = BaseProvider(enabled_baseline)
    disabled_base = BaseProvider(disabled_baseline)
    graph = GraphProvider(_graph())
    router = RepositoryCodeIntelligenceRouter(
        default_provider=disabled_base,
        providers={
            "enabled": CodeGraphAugmentedProvider(enabled_base, graph),
        },
    )

    enabled = router.analyze(enabled_request)
    disabled = router.analyze(disabled_request)

    assert enabled.semantic_graph is not None
    assert disabled is disabled_baseline
    assert graph.calls == 1


class Registry:
    def __init__(self, provider=None) -> None:
        self.provider = provider
        self.requested: list[str] = []

    def get_provider(self, provider_id: str):
        self.requested.append(provider_id)
        return self.provider


def _config(tmp_path: Path, provider_id: str) -> AppConfig:
    return AppConfig(
        source_path=tmp_path / "config.toml",
        server=ServerConfig(tmp_path / "workspaces", tmp_path / "state"),
        repositories={
            "demo": RepositoryConfig(
                repo_id="demo",
                path=tmp_path / "repo",
                code_intelligence_provider_id=provider_id,
            )
        },
    )


def test_composition_preserves_exact_baseline_when_no_repo_is_enrolled(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    base = BaseProvider(_baseline(request))

    provider = build_repository_code_intelligence(
        _config(tmp_path, ""),
        base,
        Registry(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
    )

    assert provider is base


def test_composition_attaches_unavailable_graph_for_missing_enrollment(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    baseline = _baseline(request)
    registry = Registry()
    provider = build_repository_code_intelligence(
        _config(tmp_path, "codegraph"),
        BaseProvider(baseline),
        registry,  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
    )

    result = provider.analyze(request)

    assert result.semantic_graph is not None
    assert result.semantic_graph.status is CodeIntelligenceStatus.UNAVAILABLE
    assert result.semantic_graph.provider_id == "codegraph"
    assert result.affected_tests == baseline.affected_tests
    assert registry.requested == ["codegraph"]


def test_composition_degrades_managed_state_initialization_failure(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    config = _config(tmp_path, "codegraph")
    config.server.state_root.mkdir(parents=True)
    (config.server.state_root / "providers").write_text("tampered\n", encoding="utf-8")

    provider = build_repository_code_intelligence(
        config,
        BaseProvider(_baseline(request)),
        Registry(manifest()),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
    )
    result = provider.analyze(request)

    assert result.semantic_graph is not None
    assert result.semantic_graph.status is CodeIntelligenceStatus.UNAVAILABLE
    assert any("valueerror" in item.lower() for item in result.semantic_graph.limitations)


def test_composition_uses_reviewed_manifest_identity(tmp_path: Path) -> None:
    request = _request(tmp_path)
    registry = Registry(manifest())
    provider = build_repository_code_intelligence(
        _config(tmp_path, "codegraph"),
        BaseProvider(_baseline(request)),
        registry,  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
    )

    result = provider.analyze(request)

    assert result.semantic_graph is not None
    assert result.semantic_graph.provider_version == "1.5.0"
    assert registry.requested == ["codegraph"]


def test_bootstrap_applies_repository_selection_and_preserves_override(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path, "codegraph")
    request = _request(tmp_path)

    application = build_application(config)
    provider = application.context.code_intelligence
    assert provider is not None
    result = provider.analyze(request)
    assert result.semantic_graph is not None
    assert result.semantic_graph.provider_id == "codegraph"

    baseline = BaseProvider(_baseline(request))
    overridden = build_application(
        config,
        overrides=AdapterOverrides(code_intelligence=baseline),
    )
    assert overridden.context.code_intelligence is baseline
