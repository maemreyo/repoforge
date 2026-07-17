from __future__ import annotations

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
from repoforge.domain.errors import ErrorCode, RepoForgeError


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
