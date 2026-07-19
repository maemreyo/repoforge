from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from conftest import ForgeEnvironment, git

from repoforge.adapters.code_intelligence.syntax import SyntaxCodeIntelligenceProvider
from repoforge.application.service import CodingService
from repoforge.application.workspace.assessment import (
    WorkspaceAssessmentCommand,
    WorkspaceAssessmentReader,
)
from repoforge.application.workspace.edit import FileEdit, TextEdit
from repoforge.application.workspace.run_diagnostic import WorkspaceRunDiagnosticResult
from repoforge.application.workspace.run_profile import (
    WorkspaceRunProfileBackgroundResult,
    WorkspaceRunProfileResult,
)
from repoforge.bootstrap import AdapterOverrides, build_application
from repoforge.config import load_config
from repoforge.domain.assessment import (
    AssessmentCoverage,
    AssessmentEvidenceStatus,
    WorkspaceAssessment,
    evidence,
    new_assessment_snapshot,
    validate_workspace_assessment,
)
from repoforge.domain.code_intelligence import (
    CodeIntelligenceMeasure,
    CodeIntelligenceRequest,
    CodeIntelligenceSnapshot,
    CodeIntelligenceStatus,
    new_code_intelligence_result,
)
from repoforge.domain.errors import ConfigError, ErrorCode, RepoForgeError


def test_code_intelligence_domain_is_snapshot_bound_and_normalized() -> None:
    snapshot = CodeIntelligenceSnapshot(
        repo_id="demo",
        workspace_id="workspace-1",
        head_sha="a" * 40,
        workspace_fingerprint="b" * 64,
    )
    result = new_code_intelligence_result(
        provider_id="syntax",
        provider_version="1",
        snapshot=snapshot,
        status=CodeIntelligenceStatus.CURRENT,
        coverage=CodeIntelligenceMeasure(100, "All supported files were analyzed."),
        confidence=CodeIntelligenceMeasure(90, "Imports were resolved syntactically."),
        analyzed_paths=("src/z.py", "src/a.py", "src/a.py"),
        limitations=("No runtime dispatch analysis.",),
    )
    assert result.analyzed_paths == ("src/a.py", "src/z.py")
    assert result.snapshot.snapshot_id.startswith("ci-")

    with pytest.raises(RepoForgeError) as unsafe:
        new_code_intelligence_result(
            provider_id="syntax",
            provider_version="1",
            snapshot=snapshot,
            status=CodeIntelligenceStatus.CURRENT,
            coverage=CodeIntelligenceMeasure(100, "Complete."),
            confidence=CodeIntelligenceMeasure(80, "Bounded syntax evidence."),
            analyzed_paths=("/tmp/escape.py",),
        )
    assert unsafe.value.code is ErrorCode.CODE_INTELLIGENCE_INVALID


def test_syntax_code_intelligence_maps_python_and_typescript_tests(tmp_path: Path) -> None:
    files = {
        "src/math_utils.py": "def add(a, b):\n    return a + b\n",
        "tests/test_math_utils.py": (
            "from src.math_utils import add\n\ndef test_add():\n    assert add(1, 2) == 3\n"
        ),
        "web/format.ts": ("export function formatName(name: string) { return name.trim(); }\n"),
        "web/format.test.ts": ("import { formatName } from './format';\nformatName(' Ada ');\n"),
    }
    for relative_path, content in files.items():
        path = tmp_path / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    request = CodeIntelligenceRequest(
        workspace_root=tmp_path,
        snapshot=CodeIntelligenceSnapshot(
            repo_id="demo",
            workspace_id="workspace-1",
            head_sha="a" * 40,
            workspace_fingerprint="b" * 64,
        ),
        paths=tuple(files),
        changed_paths=("src/math_utils.py", "web/format.ts"),
        diagnostic_ids=("pytest-target",),
    )
    result = SyntaxCodeIntelligenceProvider().analyze(request)

    assert result.status is CodeIntelligenceStatus.CURRENT
    assert {item.test_path for item in result.affected_tests} == {
        "tests/test_math_utils.py",
        "web/format.test.ts",
    }
    python_candidate = next(
        item for item in result.affected_tests if item.test_path == "tests/test_math_utils.py"
    )
    assert python_candidate.diagnostic_id == "pytest-target"
    assert python_candidate.selector == "tests/test_math_utils.py"
    assert any(item.resolved_path == "src/math_utils.py" for item in result.imports)
    assert any(item.resolved_path == "web/format.ts" for item in result.imports)
    assert {item.language.value for item in result.symbols} == {"python", "typescript"}


def _changed_workspace(env: ForgeEnvironment) -> str:
    created = env.service.workspace_create("demo", "assessment")
    workspace_id = created["workspace_id"]
    current = env.service.workspace_read_file(workspace_id, "hello.txt")
    env.service.workspace_edit(
        workspace_id,
        [FileEdit("hello.txt", current["sha256"], (TextEdit("hello", "assessment change"),))],
    )
    return workspace_id


def _execution_evidence() -> dict[str, object]:
    return {
        "adapter_kind": "native_reviewed",
        "identity_schema_version": 2,
        "environment_identity_hash": "c" * 64,
        "requested_policy_hash": "d" * 64,
        "effective_policy_hash": "e" * 64,
        "requested_network": "offline",
        "effective_network": "host_inherited",
        "requested_filesystem": "workspace_write",
        "effective_filesystem": "host_account_access",
        "degraded": True,
        "enforcement": {
            "network": "advisory",
            "filesystem": "advisory",
            "timeout": "enforced",
            "output": "enforced",
            "process_cleanup": "enforced",
            "cpu": "unsupported",
            "memory": "unsupported",
            "disk": "unsupported",
            "subprocess_count": "unsupported",
            "network_bytes": "unsupported",
        },
        "warnings": [],
    }


def _diagnostic_result(
    workspace_id: str, fingerprint: str, head_sha: str
) -> WorkspaceRunDiagnosticResult:
    return WorkspaceRunDiagnosticResult(
        workspace_id=workspace_id,
        diagnostic_id="pytest-target",
        summary="Run one tracked pytest path",
        selector_kind="pytest_node",
        resolved_selector="tests/test_math_utils.py",
        resolved_selectors={"selector": ["tests/test_math_utils.py"]},
        argv=["pytest", "tests/test_math_utils.py"],
        working_directory=".",
        network_policy="local_only",
        mutability="read_only",
        parser="pytest",
        intent="tdd_green",
        expectation="pass",
        expected_failure_class=None,
        returncode=0,
        outcome="passed",
        failure_class=None,
        expectation_met=True,
        business_tests_ran=True,
        valid_tdd_red_evidence=False,
        parsed={"passed": 1},
        excerpt="1 passed",
        output_truncated=False,
        fingerprint_before=fingerprint,
        fingerprint_after=fingerprint,
        fingerprint_changed=False,
        head_sha=head_sha,
        changed_paths=["src/math_utils.py"],
        unexpected_paths=[],
        change_metrics={},
        verification_invalidated=False,
        satisfies_commit_gate=False,
        next_safe_actions=[],
        execution_evidence=_execution_evidence(),
    )


def _profile_result(
    workspace_id: str, fingerprint: str, head_sha: str
) -> WorkspaceRunProfileResult:
    return WorkspaceRunProfileResult(
        workspace_id=workspace_id,
        repo_id="demo",
        profile="full",
        description="Full verification",
        verification=True,
        fingerprint=fingerprint,
        commands=[
            {
                "argv": ["pytest", "-q"],
                "returncode": 0,
                "stdout": "1 passed",
                "stderr": "",
                "stage_index": 0,
                "duration_ms": 12.5,
                "cumulative_duration_ms": 12.5,
            }
        ],
        change_metrics={},
        satisfies_commit_gate=True,
        used_default=True,
        head_sha=head_sha,
        command_source_dirty=False,
        command_source_dirty_paths=[],
        command_source_warning=None,
        completed_steps=[{"id": "step-1", "kind": "business_tests"}],
        failed_step=None,
        failure_domain=None,
        not_run_steps=[],
        business_tests_ran=True,
        valid_tdd_red_evidence=False,
        hygiene_receipt=None,
        execution_evidence=_execution_evidence(),
    )


def test_workspace_verify_plan_is_read_only_and_returns_recommendations(
    forge_env: ForgeEnvironment,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = _changed_workspace(forge_env)
    verifier = forge_env.service._verify
    monkeypatch.setattr(
        verifier._profile,
        "execute",
        lambda _command: (_ for _ in ()).throw(AssertionError("profile must not run")),
    )
    monkeypatch.setattr(
        verifier._diagnostic,
        "execute",
        lambda _command: (_ for _ in ()).throw(AssertionError("diagnostic must not run")),
    )
    monkeypatch.setattr(
        verifier._adhoc,
        "execute",
        lambda _command: (_ for _ in ()).throw(AssertionError("adhoc must not run")),
    )

    result = forge_env.service.workspace_verify(workspace_id, mode="plan")

    assert result["requested_mode"] == "plan"
    assert result["selected_mode"] == "plan"
    assert result["outcome"] == "planned"
    assert result["assessment"]["current"] is True
    assert result["recommendations"]
    assert result["commands"] == []
    from repoforge.contracts.registry import V2_TOOL_SPECS

    V2_TOOL_SPECS["workspace_verify"].validate_output(result)


def test_workspace_verify_auto_routes_high_confidence_exact_test_selector(
    forge_env: ForgeEnvironment,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_file = forge_env.source / "src" / "math_utils.py"
    test_file = forge_env.source / "tests" / "test_math_utils.py"
    source_file.parent.mkdir(parents=True, exist_ok=True)
    test_file.parent.mkdir(parents=True, exist_ok=True)
    source_file.write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    test_file.write_text(
        "from src.math_utils import add\n\ndef test_add():\n    assert add(1, 2) == 3\n",
        encoding="utf-8",
    )
    git("add", "src/math_utils.py", "tests/test_math_utils.py", cwd=forge_env.source)
    git("commit", "-m", "add verify routing fixture", cwd=forge_env.source)
    git("push", "origin", "main", cwd=forge_env.source)
    workspace_id = forge_env.service.workspace_create("demo", "verify targeted")["workspace_id"]
    current = forge_env.service.workspace_read_file(workspace_id, "src/math_utils.py")
    forge_env.service.workspace_write_file(
        workspace_id,
        "src/math_utils.py",
        "def add(a, b):\n    return a + b + 0\n",
        current["sha256"],
    )
    status = forge_env.service.workspace_status(workspace_id)
    captured: list[Any] = []

    def run_diagnostic(command: Any) -> WorkspaceRunDiagnosticResult:
        captured.append(command)
        return _diagnostic_result(workspace_id, status["workspace_fingerprint"], status["head_sha"])

    monkeypatch.setattr(forge_env.service._verify._diagnostic, "execute", run_diagnostic)
    monkeypatch.setattr(
        forge_env.service._verify._profile,
        "execute",
        lambda _command: (_ for _ in ()).throw(AssertionError("full profile must not run")),
    )

    result = forge_env.service.workspace_verify(
        workspace_id,
        mode="auto",
        intent="tdd_green",
        expectation="pass",
    )

    assert result["selected_mode"] == "diagnostic"
    assert captured[0].diagnostic_id == "pytest-target"
    assert captured[0].selector == ["tests/test_math_utils.py"]
    assert captured[0].intent == "tdd_green"
    assert result["outcome"] == "passed"
    assert result["satisfies_commit_gate"] is False


def test_workspace_verify_auto_falls_back_to_final_profile_when_intelligence_unavailable(
    forge_env: ForgeEnvironment,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingProvider:
        provider_id = "failing"
        provider_version = "1"

        def analyze(self, request: CodeIntelligenceRequest):
            del request
            raise RuntimeError("provider unavailable")

    config = load_config(forge_env.config_path)
    application = build_application(
        config,
        overrides=AdapterOverrides(code_intelligence=FailingProvider()),
    )
    service = CodingService(config, application=application)
    workspace_id = service.workspace_create("demo", "verify fallback")["workspace_id"]
    status = service.workspace_status(workspace_id)
    captured: list[Any] = []

    def run_profile(command: Any) -> WorkspaceRunProfileResult:
        captured.append(command)
        return _profile_result(workspace_id, status["workspace_fingerprint"], status["head_sha"])

    monkeypatch.setattr(service._verify._profile, "execute", run_profile)
    monkeypatch.setattr(
        service._verify._diagnostic,
        "execute",
        lambda _command: (_ for _ in ()).throw(AssertionError("diagnostic must not run")),
    )

    result = service.workspace_verify(workspace_id, mode="auto")

    assert result["selected_mode"] == "profile"
    assert result["outcome"] == "fallback_full"
    assert captured[0].profile_name == "full"
    assert "unavailable" in result["routing_reason"].lower()


def test_workspace_verify_explicit_diagnostic_forwards_intent_and_reuse_controls(
    forge_env: ForgeEnvironment,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = _changed_workspace(forge_env)
    status = forge_env.service.workspace_status(workspace_id)
    captured: list[Any] = []

    def run_diagnostic(command: Any) -> WorkspaceRunDiagnosticResult:
        captured.append(command)
        return _diagnostic_result(workspace_id, status["workspace_fingerprint"], status["head_sha"])

    monkeypatch.setattr(forge_env.service._verify._diagnostic, "execute", run_diagnostic)
    result = forge_env.service.workspace_verify(
        workspace_id,
        mode="diagnostic",
        diagnostic_id="pytest-target",
        selector=["tests/test_math_utils.py"],
        intent="tdd_red",
        expectation="fail",
        expected_failure_class="test_failure",
        force_rerun=True,
    )

    command = captured[0]
    assert command.intent == "tdd_red"
    assert command.expectation == "fail"
    assert command.expected_failure_class == "test_failure"
    assert command.force_rerun is True
    assert result["selected_mode"] == "diagnostic"


def test_workspace_verify_uses_refresh_impact_paths_instead_of_dirty_paths(
    forge_env: ForgeEnvironment,
) -> None:
    files = {
        "src/one.py": "def one():\n    return 1\n",
        "tests/test_one.py": "from src.one import one\n\ndef test_one():\n    assert one() == 1\n",
        "src/two.py": "def two():\n    return 2\n",
        "tests/test_two.py": "from src.two import two\n\ndef test_two():\n    assert two() == 2\n",
    }
    for relative_path, content in files.items():
        path = forge_env.source / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    git("add", *files, cwd=forge_env.source)
    git("commit", "-m", "add refresh impact fixtures", cwd=forge_env.source)
    git("push", "origin", "main", cwd=forge_env.source)
    workspace_id = forge_env.service.workspace_create("demo", "verify impact selector")[
        "workspace_id"
    ]
    current = forge_env.service.workspace_read_file(workspace_id, "src/one.py")
    forge_env.service.workspace_write_file(
        workspace_id,
        "src/one.py",
        "def one():\n    return 10\n",
        current["sha256"],
    )

    result = forge_env.service.workspace_verify(
        workspace_id,
        mode="plan",
        impact_paths=("src/two.py",),
    )

    assert result["assessment"]["changed_paths"] == ["src/two.py"]
    assert any(item.get("selector") == "tests/test_two.py" for item in result["recommendations"])
    assert all(item.get("selector") != "tests/test_one.py" for item in result["recommendations"])


def test_workspace_verify_staleness_warning_does_not_block_execution(
    forge_env: ForgeEnvironment,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = _changed_workspace(forge_env)
    verifier = forge_env.service._verify
    assessment = verifier._assessment.execute(WorkspaceAssessmentCommand(workspace_id))
    stale_base = replace(
        assessment.base_freshness,
        value={**assessment.base_freshness.value, "refresh_required": True, "behind_base": 3},
    )
    stale_assessment = replace(assessment, base_freshness=stale_base)
    monkeypatch.setattr(verifier._assessment, "execute", lambda _command: stale_assessment)
    captured: list[Any] = []

    def run_profile(command: Any) -> WorkspaceRunProfileResult:
        captured.append(command)
        return _profile_result(
            workspace_id,
            assessment.snapshot.workspace_fingerprint,
            assessment.snapshot.head_sha,
        )

    monkeypatch.setattr(verifier._profile, "execute", run_profile)
    result = forge_env.service.workspace_verify(workspace_id, mode="profile", profile_name="full")

    assert captured
    assert "3 commit(s) behind" in result["staleness_warning"]
    assert result["outcome"] == "passed"
    assert result["steps"][0]["status"] == "completed"
    assert result["steps"][0]["duration_ms"] == 12.5


def test_workspace_verify_journaled_artifact_invalidates_commit_gate(
    forge_env: ForgeEnvironment,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = _changed_workspace(forge_env)
    status = forge_env.service.workspace_status(workspace_id)
    monkeypatch.setattr(
        forge_env.service._verify._profile,
        "execute",
        lambda _command: _profile_result(
            workspace_id,
            status["workspace_fingerprint"],
            status["head_sha"],
        ),
    )

    result = forge_env.service.workspace_verify(
        workspace_id,
        mode="profile",
        profile_name="full",
        artifact_output_path="verify-result.json",
    )

    workspace_path = Path(forge_env.service.workspace_status(workspace_id)["path"])
    decoded = json.loads((workspace_path / "verify-result.json").read_text(encoding="utf-8"))
    assert decoded["selected_mode"] == "profile"
    assert result["artifact_paths"] == ["verify-result.json"]
    assert result["satisfies_commit_gate"] is False
    assert result["workspace_fingerprint"] != status["workspace_fingerprint"]


def test_workspace_verify_preserves_durable_profile_background_operation(
    forge_env: ForgeEnvironment,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = _changed_workspace(forge_env)
    monkeypatch.setattr(
        forge_env.service._verify._profile,
        "execute",
        lambda _command: WorkspaceRunProfileBackgroundResult(
            operation_id="op-verify-background-0001",
            phase="running",
            safe_next_action="Poll operation_status.",
        ),
    )

    result = forge_env.service.workspace_verify(
        workspace_id,
        mode="profile",
        profile_name="full",
        background=True,
    )

    assert result["outcome"] == "running"
    assert result["operation"]["operation_id"] == "op-verify-background-0001"
    assert result["operation"]["kind"] == "workspace_run_profile"
    from repoforge.contracts.registry import V2_TOOL_SPECS

    V2_TOOL_SPECS["workspace_verify"].validate_output(result)


def test_workspace_verify_rejects_background_artifact_output(
    forge_env: ForgeEnvironment,
) -> None:
    workspace_id = _changed_workspace(forge_env)

    with pytest.raises(ConfigError, match="Background workspace_verify"):
        forge_env.service.workspace_verify(
            workspace_id,
            mode="profile",
            background=True,
            artifact_output_path="verify-result.json",
        )


def test_assessment_includes_affected_test_candidates_and_targeted_stage(
    forge_env: ForgeEnvironment,
) -> None:
    source_file = forge_env.source / "src" / "math_utils.py"
    test_file = forge_env.source / "tests" / "test_math_utils.py"
    source_file.parent.mkdir(parents=True, exist_ok=True)
    test_file.parent.mkdir(parents=True, exist_ok=True)
    source_file.write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    test_file.write_text(
        "from src.math_utils import add\n\ndef test_add():\n    assert add(1, 2) == 3\n",
        encoding="utf-8",
    )
    git("add", "src/math_utils.py", "tests/test_math_utils.py", cwd=forge_env.source)
    git("commit", "-m", "add code intelligence fixture", cwd=forge_env.source)
    git("push", "origin", "main", cwd=forge_env.source)

    workspace_id = forge_env.service.workspace_create("demo", "assessment intelligence")[
        "workspace_id"
    ]
    current = forge_env.service.workspace_read_file(workspace_id, "src/math_utils.py")
    forge_env.service.workspace_write_file(
        workspace_id,
        "src/math_utils.py",
        "def add(a, b):\n    return a + b + 0\n",
        current["sha256"],
    )

    result = forge_env.service.workspace_assessment(workspace_id)
    intelligence = result["code_intelligence"]
    assert intelligence["status"] == "current"
    candidates = intelligence["value"]["affected_tests"]
    assert candidates[0]["test_path"] == "tests/test_math_utils.py"
    assert candidates[0]["diagnostic_id"] == "pytest-target"
    assert any(
        stage.get("selector") == "tests/test_math_utils.py"
        for stage in result["verification_recommendation"]["ordered_stages"]
    )


def test_assessment_degrades_code_intelligence_provider_failure(
    forge_env: ForgeEnvironment,
) -> None:
    class FailingProvider:
        provider_id = "failing"
        provider_version = "1"

        def analyze(self, request: CodeIntelligenceRequest):
            del request
            raise RuntimeError("provider unavailable")

    config = load_config(forge_env.config_path)
    application = build_application(
        config,
        overrides=AdapterOverrides(code_intelligence=FailingProvider()),
    )
    service = CodingService(config, application=application)
    workspace_id = service.workspace_create("demo", "assessment intelligence fallback")[
        "workspace_id"
    ]

    result = service.workspace_assessment(workspace_id)
    intelligence = result["code_intelligence"]
    assert intelligence["status"] == "unavailable"
    assert intelligence["coverage"] == "none"
    assert intelligence["error_code"] == ErrorCode.CODE_INTELLIGENCE_UNAVAILABLE.value
    assert any(item.startswith("code_intelligence:") for item in result["uncertainties"])


def test_failed_profile_surfaces_affected_test_selector(
    forge_env: ForgeEnvironment,
) -> None:
    source_file = forge_env.source / "src" / "math_utils.py"
    test_file = forge_env.source / "tests" / "test_math_utils.py"
    source_file.parent.mkdir(parents=True, exist_ok=True)
    test_file.parent.mkdir(parents=True, exist_ok=True)
    source_file.write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    test_file.write_text(
        "from src.math_utils import add\n\ndef test_add():\n    assert add(1, 2) == 3\n",
        encoding="utf-8",
    )
    git("add", "src/math_utils.py", "tests/test_math_utils.py", cwd=forge_env.source)
    git("commit", "-m", "add profile intelligence fixture", cwd=forge_env.source)
    git("push", "origin", "main", cwd=forge_env.source)
    config_text = forge_env.config_path.read_text(encoding="utf-8")
    forge_env.config_path.write_text(
        config_text
        + """

[repositories.demo.profiles.static-failure]
description = "Fail after static analysis"
verification = true
commands = [["python3", "-c", "import sys; sys.exit(3)"]]

[[repositories.demo.profiles.static-failure.steps]]
id = "static"
kind = "static_analysis"
command = ["python3", "-c", "import sys; sys.exit(3)"]
""",
        encoding="utf-8",
    )
    service = CodingService(load_config(forge_env.config_path))
    workspace_id = service.workspace_create("demo", "profile intelligence")["workspace_id"]
    current = service.workspace_read_file(workspace_id, "src/math_utils.py")
    service.workspace_write_file(
        workspace_id,
        "src/math_utils.py",
        "def add(a, b):\n    return a + b + 0\n",
        current["sha256"],
    )

    with pytest.raises(RepoForgeError) as failure:
        service.workspace_run_profile(workspace_id, "static-failure")

    candidates = failure.value.details["affected_test_candidates"]
    assert candidates[0]["selector"] == "tests/test_math_utils.py"
    assert "tests/test_math_utils.py" in (failure.value.safe_next_action or "")
    assert "workspace_run_diagnostic" in (failure.value.safe_next_action or "")


def test_assessment_domain_rejects_mixed_snapshot_components() -> None:
    snapshot = new_assessment_snapshot(
        workspace_id="workspace-1",
        head_sha="a" * 40,
        workspace_fingerprint="b" * 64,
        config_generation="c" * 64,
        policy_hash="d" * 64,
        created_at="2026-07-14T00:00:00+00:00",
    )
    component = evidence(
        snapshot,
        status=AssessmentEvidenceStatus.CURRENT,
        coverage=AssessmentCoverage.COMPLETE,
        value={"ok": True},
    )
    components = {
        name: component
        for name in (
            "changed_paths",
            "diff_summary",
            "change_budget",
            "path_policy",
            "code_intelligence",
            "base_freshness",
            "pr_state",
            "ci_summary",
            "failure_evidence_refs",
            "receipt_freshness",
        )
    }
    assessment = WorkspaceAssessment(
        snapshot=snapshot,
        evidence_coverage={name: "complete" for name in components},
        uncertainties=(),
        **components,
    )
    assert validate_workspace_assessment(assessment) == assessment

    other = replace(component, snapshot_id="e" * 64)
    with pytest.raises(RepoForgeError) as mixed:
        validate_workspace_assessment(replace(assessment, ci_summary=other))
    assert mixed.value.code is ErrorCode.ASSESSMENT_INVALID


def test_assessment_is_deterministic_read_only_and_explicit_about_gaps(
    forge_env: ForgeEnvironment,
) -> None:
    workspace_id = _changed_workspace(forge_env)
    before = forge_env.service.application.context.store.load(workspace_id)
    assert before.last_verification is None

    first = forge_env.service.workspace_assessment(workspace_id)
    second = forge_env.service.workspace_assessment(workspace_id)

    assert first["current"] is True
    for field in (
        "workspace_id",
        "head_sha",
        "workspace_fingerprint",
        "config_generation",
        "policy_hash",
    ):
        assert first["snapshot"][field] == second["snapshot"][field]
    assert first["changed_paths"]["value"]["paths"] == ["hello.txt"]
    assert first["diff_summary"]["value"]["stat"]
    assert first["change_budget"]["coverage"] == "complete"
    assert first["path_policy"]["value"]["violations"] == []
    assert tuple(first["evidence_coverage"]) == tuple(sorted(first["evidence_coverage"]))
    assert first["pr_state"]["status"] in {"current", "not_applicable"}
    for result in (first, second):
        assert result["risk"]["assessment_snapshot_id"] == result["snapshot"]["snapshot_id"]
        assert (
            result["verification_recommendation"]["assessment_snapshot_id"]
            == result["snapshot"]["snapshot_id"]
        )
        assert result["verification_recommendation"]["required_profiles"][-1] == "full"
    assert {
        key: value for key, value in first["risk"].items() if key != "assessment_snapshot_id"
    } == {key: value for key, value in second["risk"].items() if key != "assessment_snapshot_id"}
    assert {
        key: value
        for key, value in first["verification_recommendation"].items()
        if key != "assessment_snapshot_id"
    } == {
        key: value
        for key, value in second["verification_recommendation"].items()
        if key != "assessment_snapshot_id"
    }

    after = forge_env.service.application.context.store.load(workspace_id)
    assert after.last_verification is None


def test_assessment_detects_workspace_mutation_after_provider_boundary(
    forge_env: ForgeEnvironment,
) -> None:
    workspace_id = _changed_workspace(forge_env)
    reader = WorkspaceAssessmentReader(forge_env.service.application.context)
    original = reader._diff.execute
    workspace_path = Path(forge_env.service.workspace_status(workspace_id)["path"])

    def mutate(command: Any) -> Any:
        result = original(command)
        (workspace_path / "boundary.txt").write_text("changed\n", encoding="utf-8")
        return result

    reader._diff.execute = mutate  # type: ignore[method-assign]
    with pytest.raises(RepoForgeError) as stale:
        reader.execute(WorkspaceAssessmentCommand(workspace_id))
    assert stale.value.code is ErrorCode.STALE_ASSESSMENT_SNAPSHOT


def test_assessment_detects_configuration_generation_change(
    forge_env: ForgeEnvironment,
) -> None:
    workspace_id = _changed_workspace(forge_env)
    reader = WorkspaceAssessmentReader(forge_env.service.application.context)
    original = reader._status.execute
    config_path = forge_env.config_path
    original_config = config_path.read_text(encoding="utf-8")

    def mutate(command: Any) -> Any:
        result = original(command)
        config_path.write_text(original_config + "\n# changed generation\n", encoding="utf-8")
        return result

    reader._status.execute = mutate  # type: ignore[method-assign]
    try:
        with pytest.raises(RepoForgeError) as stale:
            reader.execute(WorkspaceAssessmentCommand(workspace_id))
        assert stale.value.code is ErrorCode.STALE_ASSESSMENT_SNAPSHOT
    finally:
        config_path.write_text(original_config, encoding="utf-8")


def test_assessment_preserves_partial_provider_failure(
    forge_env: ForgeEnvironment,
) -> None:
    workspace_id = _changed_workspace(forge_env)
    reader = WorkspaceAssessmentReader(forge_env.service.application.context)

    def unavailable(_command: Any) -> Any:
        raise RepoForgeError(
            "GitHub unavailable",
            code=ErrorCode.COMMAND_FAILED,
            retryable=True,
        )

    reader._pr.execute = unavailable  # type: ignore[method-assign]
    result = reader.execute(WorkspaceAssessmentCommand(workspace_id))
    assert result.current is True
    assert result.pr_state.status is AssessmentEvidenceStatus.NOT_APPLICABLE
    assert result.pr_state.coverage is AssessmentCoverage.NONE
    assert result.pr_state.error_code == ErrorCode.COMMAND_FAILED.value
    assert any(item.startswith("pr_state:") for item in result.uncertainties)
