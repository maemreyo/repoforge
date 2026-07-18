from __future__ import annotations

from repoforge.contracts.registry import V2_TOOL_NAMES, V2_TOOL_SPECS


def test_workspace_verify_is_a_first_class_v2_contract() -> None:
    spec = V2_TOOL_SPECS["workspace_verify"]
    fields = set(spec.input_model.model_fields)
    schema = spec.input_model.model_json_schema()

    assert {
        "mode",
        "intent",
        "expectation",
        "force_rerun",
        "impact_paths",
        "artifact_output_path",
    } <= fields
    assert set(schema["$defs"]["VerifyMode"]["enum"]) == {
        "plan",
        "auto",
        "diagnostic",
        "profile",
        "adhoc",
    }
    assert "deprecated" not in str(schema).lower()


def test_mcp_tool_surface_is_static_reviewed_and_unique() -> None:
    assert len(V2_TOOL_NAMES) == 28
    assert len(V2_TOOL_NAMES) == len(set(V2_TOOL_NAMES))
    assert tuple(V2_TOOL_SPECS) == V2_TOOL_NAMES
    assert {
        "workspace_verify",
        "workspace_format_changed",
        "workspace_refresh",
        "config_inspect",
        "runtime_logs_read",
        "repo_policy",
        "operation",
    }.issubset(V2_TOOL_NAMES)
    assert {
        "workspace_run_diagnostic",
        "workspace_run_profile",
        "workspace_refresh_preview",
        "repo_policy_apply",
        "operation_status",
    }.isdisjoint(V2_TOOL_NAMES)
