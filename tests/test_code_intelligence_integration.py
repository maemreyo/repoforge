from __future__ import annotations

import pytest
from conftest import ForgeEnvironment, git

from repoforge.adapters.code_intelligence.syntax import SyntaxCodeIntelligenceProvider
from repoforge.application.code_intelligence import (
    CodeIntelligenceAnalyzer,
    CodeIntelligenceCommand,
)
from repoforge.application.service import CodingService
from repoforge.bootstrap import AdapterOverrides, build_application
from repoforge.config import load_config
from repoforge.domain.code_intelligence import (
    CodeIntelligenceMeasure,
    CodeIntelligenceRequest,
    CodeIntelligenceStatus,
    new_code_intelligence_result,
)
from repoforge.domain.errors import ErrorCode, RepoForgeError


def _service_with_provider(forge_env: ForgeEnvironment, provider: object) -> CodingService:
    config = load_config(forge_env.config_path)
    application = build_application(
        config,
        overrides=AdapterOverrides(code_intelligence=provider),
    )
    return CodingService(config, application=application)


def test_analyzer_detects_provider_side_effect_and_rejects_stale_snapshot(
    forge_env: ForgeEnvironment,
) -> None:
    class MutatingProvider:
        provider_id = "mutating"
        provider_version = "1"

        def analyze(self, request: CodeIntelligenceRequest):
            result = SyntaxCodeIntelligenceProvider().analyze(request)
            (request.workspace_root / "provider-side-effect.txt").write_text(
                "unexpected\n",
                encoding="utf-8",
            )
            return result

    service = _service_with_provider(forge_env, MutatingProvider())
    workspace_id = service.workspace_create("demo", "intelligence stale snapshot")["workspace_id"]
    before = service.workspace_status(workspace_id)

    with pytest.raises(RepoForgeError) as stale:
        CodeIntelligenceAnalyzer(service.application.context).execute(
            CodeIntelligenceCommand(
                workspace_id,
                expected_head_sha=before["head_sha"],
                expected_fingerprint=before["workspace_fingerprint"],
            )
        )

    assert stale.value.code is ErrorCode.CODE_INTELLIGENCE_STALE
    assert stale.value.retryable is True
    assert (
        service.workspace_status(workspace_id)["workspace_fingerprint"]
        != before["workspace_fingerprint"]
    )


def test_policy_denied_paths_never_reach_the_provider(
    forge_env: ForgeEnvironment,
) -> None:
    class RecordingProvider:
        provider_id = "recording"
        provider_version = "1"

        def __init__(self) -> None:
            self.requests: list[CodeIntelligenceRequest] = []

        def analyze(self, request: CodeIntelligenceRequest):
            self.requests.append(request)
            return SyntaxCodeIntelligenceProvider().analyze(request)

    secret = forge_env.source / ".env"
    source = forge_env.source / "service.py"
    secret.write_text("TOKEN=not-for-provider\n", encoding="utf-8")
    source.write_text("def service():\n    return True\n", encoding="utf-8")
    git("add", "-f", ".env", "service.py", cwd=forge_env.source)
    git("commit", "-m", "add denied intelligence fixture", cwd=forge_env.source)
    git("push", "origin", "main", cwd=forge_env.source)

    provider = RecordingProvider()
    service = _service_with_provider(forge_env, provider)
    workspace_id = service.workspace_create("demo", "intelligence denied paths")["workspace_id"]
    current = service.workspace_read_file(workspace_id, "service.py")
    service.workspace_write_file(
        workspace_id,
        "service.py",
        "def service():\n    return False\n",
        current["sha256"],
    )
    result = service.workspace_assessment(workspace_id)
    intelligence = result["code_intelligence"]

    assert provider.requests
    assert ".env" not in provider.requests[0].paths
    assert intelligence["value"]["denied_paths"] == []
    assert "TOKEN" not in repr(intelligence)


def test_provider_snapshot_mismatch_degrades_to_unavailable_without_false_facts(
    forge_env: ForgeEnvironment,
) -> None:
    class MismatchedProvider:
        provider_id = "mismatched"
        provider_version = "1"

        def analyze(self, request: CodeIntelligenceRequest):
            wrong_snapshot = type(request.snapshot)(
                repo_id=request.snapshot.repo_id,
                workspace_id=request.snapshot.workspace_id,
                head_sha="f" * 40,
                workspace_fingerprint=request.snapshot.workspace_fingerprint,
            )
            return new_code_intelligence_result(
                provider_id=self.provider_id,
                provider_version=self.provider_version,
                snapshot=wrong_snapshot,
                status=CodeIntelligenceStatus.CURRENT,
                coverage=CodeIntelligenceMeasure(100, "Incorrect snapshot fixture."),
                confidence=CodeIntelligenceMeasure(100, "Incorrect snapshot fixture."),
                analyzed_paths=("hello.txt",),
            )

    service = _service_with_provider(forge_env, MismatchedProvider())
    workspace_id = service.workspace_create("demo", "intelligence mismatch fallback")[
        "workspace_id"
    ]
    result = service.workspace_assessment(workspace_id)
    intelligence = result["code_intelligence"]

    assert intelligence["status"] == "unavailable"
    assert intelligence["coverage"] == "none"
    assert intelligence["value"] == {}
    assert intelligence["error_code"] == ErrorCode.CODE_INTELLIGENCE_UNAVAILABLE.value


def test_default_analysis_is_read_only_on_unchanged_workspace(
    forge_env: ForgeEnvironment,
) -> None:
    workspace_id = forge_env.service.workspace_create("demo", "intelligence read only")[
        "workspace_id"
    ]
    before = forge_env.service.workspace_status(workspace_id)
    analysis = CodeIntelligenceAnalyzer(forge_env.service.application.context).execute(
        CodeIntelligenceCommand(
            workspace_id,
            expected_head_sha=before["head_sha"],
            expected_fingerprint=before["workspace_fingerprint"],
        )
    )
    after = forge_env.service.workspace_status(workspace_id)

    assert analysis.result.snapshot.head_sha == before["head_sha"]
    assert after["workspace_fingerprint"] == before["workspace_fingerprint"]
    assert after["changed_paths"] == before["changed_paths"]
