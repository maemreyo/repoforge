from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from conftest import ForgeEnvironment

from repoforge.application.workspace.assessment import (
    WorkspaceAssessmentCommand,
    WorkspaceAssessmentReader,
)
from repoforge.application.workspace.edit import FileEdit, TextEdit
from repoforge.domain.assessment import (
    AssessmentCoverage,
    AssessmentEvidenceStatus,
    WorkspaceAssessment,
    evidence,
    new_assessment_snapshot,
    validate_workspace_assessment,
)
from repoforge.domain.errors import ErrorCode, RepoForgeError


def _changed_workspace(env: ForgeEnvironment) -> str:
    created = env.service.workspace_create("demo", "assessment")
    workspace_id = created["workspace_id"]
    current = env.service.workspace_read_file(workspace_id, "hello.txt")
    env.service.workspace_edit(
        workspace_id,
        [FileEdit("hello.txt", current["sha256"], (TextEdit("hello", "assessment change"),))],
    )
    return workspace_id


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
