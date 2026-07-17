from __future__ import annotations

import ast
from pathlib import Path


def test_workspace_verify_mcp_surface_is_no_longer_a_deprecated_profile_alias() -> None:
    path = Path(__file__).parents[1] / "src" / "repoforge" / "interfaces" / "mcp" / "server.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    create = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "create_server"
    )
    verify = next(
        node
        for node in create.body
        if isinstance(node, ast.FunctionDef) and node.name == "workspace_verify"
    )
    arguments = {argument.arg for argument in verify.args.args}
    source = ast.get_source_segment(path.read_text(encoding="utf-8"), verify) or ""

    assert {
        "mode",
        "intent",
        "expectation",
        "force_rerun",
        "impact_paths",
        "artifact_output_path",
    } <= arguments
    assert "deprecated alias" not in source
    assert 'Literal["plan", "auto", "diagnostic", "profile", "adhoc"]' in source


def test_mcp_tool_surface_remains_reviewed_and_unique() -> None:
    path = Path(__file__).parents[1] / "src" / "repoforge" / "interfaces" / "mcp" / "server.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    create = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "create_server"
    )
    tools = [
        node.name
        for node in create.body
        if isinstance(node, ast.FunctionDef)
        and any(
            isinstance(decorator, ast.Call)
            and isinstance(decorator.func, ast.Attribute)
            and decorator.func.attr == "tool"
            for decorator in node.decorator_list
        )
    ]
    assert len(tools) == 53
    assert len(tools) == len(set(tools))
    assert {
        "workspace_run_diagnostic",
        "workspace_hygiene_status",
        "workspace_format_changed",
        "workspace_base_status",
        "workspace_refresh",
        "workspace_refresh_preview",
        "config_inspect",
        "runtime_logs_read",
        "repo_policy_apply",
    }.issubset(tools)
