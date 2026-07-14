from __future__ import annotations

import ast
from pathlib import Path


def test_phase5_mcp_tool_surface_remains_39_tools() -> None:
    path = Path(__file__).parents[1] / "src" / "repoforge" / "interfaces" / "mcp" / "server.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    create = next(
        n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "create_server"
    )
    tools = [
        n.name
        for n in create.body
        if isinstance(n, ast.FunctionDef)
        and any(
            isinstance(d, ast.Call)
            and isinstance(d.func, ast.Attribute)
            and (d.func.attr == "tool")
            for d in n.decorator_list
        )
    ]
    assert len(tools) == 39
    assert len(tools) == len(set(tools))
