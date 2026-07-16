from __future__ import annotations

import ast
from pathlib import Path


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
    assert len(tools) == 50
    assert len(tools) == len(set(tools))
    assert {
        "workspace_run_diagnostic",
        "workspace_hygiene_status",
        "workspace_format_changed",
        "workspace_base_status",
        "workspace_refresh",
        "workspace_refresh_preview",
    }.issubset(tools)
