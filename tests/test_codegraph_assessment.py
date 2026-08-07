from __future__ import annotations

from conftest import TEST_CONFIG_GENERATION, ForgeEnvironment

from repoforge.application.service import CodingService
from repoforge.bootstrap import AdapterOverrides, build_application
from repoforge.config import load_config
from repoforge.domain.code_intelligence import (
    CodeIntelligenceMeasure,
    CodeIntelligenceRequest,
    CodeIntelligenceStatus,
    SemanticGraphEvidence,
    new_code_intelligence_result,
)


class PartialSemanticProvider:
    provider_id = "tree-sitter"
    provider_version = "1"

    def analyze(self, request: CodeIntelligenceRequest):
        graph = SemanticGraphEvidence(
            provider_id="codegraph",
            provider_version="1.5.0",
            status=CodeIntelligenceStatus.PARTIAL,
            coverage=CodeIntelligenceMeasure(80, "Projection excluded reviewed paths."),
            confidence=CodeIntelligenceMeasure(90, "Semantic evidence is bounded."),
            limitations=("Semantic projection is incomplete.",),
            truncated=True,
        )
        analyzed = request.paths[:1]
        return new_code_intelligence_result(
            provider_id=self.provider_id,
            provider_version=self.provider_version,
            snapshot=request.snapshot,
            status=CodeIntelligenceStatus.CURRENT,
            coverage=CodeIntelligenceMeasure(100, "Baseline evidence is complete."),
            confidence=CodeIntelligenceMeasure(100, "Baseline evidence is calibrated."),
            analyzed_paths=analyzed,
            semantic_graph=graph,
        )


def test_assessment_serializes_semantic_routing_state(
    forge_env: ForgeEnvironment,
) -> None:
    config = load_config(forge_env.config_path)
    application = build_application(
        config,
        overrides=AdapterOverrides(code_intelligence=PartialSemanticProvider()),
        config_generation=TEST_CONFIG_GENERATION,
    )
    service = CodingService(config, application=application)
    workspace_id = service.workspace_create("demo", "semantic assessment")["workspace_id"]

    result = service.workspace_assessment(workspace_id)
    intelligence = result["code_intelligence"]["value"]

    assert intelligence["semantic_status"] == "partial"
    assert intelligence["semantic_targeting_allowed"] is False
    assert "final profile" in intelligence["semantic_widening_reason"].lower()
