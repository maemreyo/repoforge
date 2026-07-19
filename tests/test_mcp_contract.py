from __future__ import annotations

import json

import pytest
from conftest import ForgeEnvironment
from mcp.shared.memory import create_connected_server_and_client_session

from repoforge.contracts.registry import V2_TOOL_NAMES, V2_TOOL_SPECS
from repoforge.interfaces.mcp.contract import build_release_contract
from repoforge.interfaces.mcp.server import (
    FORGE_V2_IDENTITY,
    create_server,
    tool_surface_hash,
)


def test_tool_surface_hash_is_deterministic_and_v2_only() -> None:
    assert tool_surface_hash() == tool_surface_hash(2)
    assert len(tool_surface_hash()) == 64
    with pytest.raises(ValueError, match="only supports contract v2"):
        tool_surface_hash(1)


@pytest.mark.anyio
async def test_release_contract_is_big_bang_v2_with_one_grace_tool() -> None:
    contract = await build_release_contract()
    mcp = contract["mcp"]

    assert contract["contract_version"] == 2
    assert mcp["identity"] == "forge_v2"
    assert mcp["retired_identity"] == "forge_v1"
    assert mcp["tool_count"] == 28
    assert mcp["tool_names"] == list(V2_TOOL_NAMES)
    assert set(mcp["tool_hashes"]) == set(V2_TOOL_NAMES)
    assert "tools" not in mcp
    assert mcp["tool_contract"] == {
        "current_version": 2,
        "supported_versions": [2],
        "aliases": [],
    }
    assert mcp["grace"]["identity"] == "forge_v1"
    assert mcp["grace"]["tool_count"] == 1
    assert mcp["grace"]["tool_names"] == ["migration_required"]
    assert set(mcp["grace"]["tool_hashes"]) == {"migration_required"}


@pytest.mark.anyio
async def test_mcp_protocol_contract_and_annotations(
    forge_env: ForgeEnvironment,
) -> None:
    server = create_server(service=forge_env.service)
    assert server.name == FORGE_V2_IDENTITY
    tools = await server.list_tools()

    assert tuple(tool.name for tool in tools) == V2_TOOL_NAMES
    assert len(tools) == 28
    assert len({tool.name for tool in tools}) == 28
    assert {"repo_status", "workspace_run_profile", "repo_policy_apply"}.isdisjoint(
        tool.name for tool in tools
    )
    for tool in tools:
        spec = V2_TOOL_SPECS[tool.name]
        assert tool.inputSchema == spec.input_model.model_json_schema(mode="validation")
        assert tool.outputSchema == spec.output_model.model_json_schema(mode="validation")
        assert tool.annotations is not None

    by_name = {tool.name: tool for tool in tools}
    assert by_name["repo_read"].annotations.readOnlyHint is True
    assert by_name["repo_read"].annotations.openWorldHint is False
    assert by_name["repo_pr_read"].annotations.openWorldHint is True
    assert by_name["workspace_remove"].annotations.destructiveHint is True
    # workspace_mutate can run delete/restore operations that irreversibly
    # discard content, so it is not honestly annotated non-destructive.
    assert by_name["workspace_mutate"].annotations.destructiveHint is True
    # workspace_verify's mode=plan/plan_action=create sub-mode allocates a
    # fresh, distinct plan_id on every call, so the tool as a whole is not
    # idempotent even though its read-only sub-modes are (#225 round-3
    # review). MCP annotations are per-tool, not per-mode.
    assert by_name["workspace_verify"].annotations.idempotentHint is False
    assert by_name["operation"].annotations.idempotentHint is True
    assert by_name["workspace_push"].annotations.openWorldHint is True


@pytest.mark.anyio
async def test_representative_v2_tools_execute_through_protocol(
    forge_env: ForgeEnvironment,
) -> None:
    server = create_server(service=forge_env.service)
    async with create_connected_server_and_client_session(server) as session:
        listed = await session.call_tool("repo_list", {})
        assert listed.isError is False
        assert listed.structuredContent is not None
        V2_TOOL_SPECS["repo_list"].validate_output(listed.structuredContent)

        read = await session.call_tool(
            "repo_read",
            {"repo_id": "demo", "files": [{"path": "README.md"}]},
        )
        read_error = (
            json.loads("\n".join(getattr(item, "text", "") for item in read.content))
            if read.isError
            else {}
        )
        assert read.isError is False, read_error.get("error")
        assert read.structuredContent is not None
        V2_TOOL_SPECS["repo_read"].validate_output(read.structuredContent)

        searched = await session.call_tool(
            "repo_search",
            {"repo_id": "demo", "query": "Repository", "context_lines": 1},
        )
        assert searched.isError is False
        assert searched.structuredContent is not None
        V2_TOOL_SPECS["repo_search"].validate_output(searched.structuredContent)

        created = await session.call_tool(
            "workspace_create",
            {
                "repo_id": "demo",
                "task_slug": "v2 protocol",
                "idempotency_key": "v2-protocol-create-0001",
            },
        )
        assert created.isError is False
        assert created.structuredContent is not None
        V2_TOOL_SPECS["workspace_create"].validate_output(created.structuredContent)
        workspace_id = created.structuredContent["workspace_id"]

        status = await session.call_tool(
            "workspace_status",
            {"workspace_id": workspace_id},
        )
        assert status.isError is False
        assert status.structuredContent is not None
        V2_TOOL_SPECS["workspace_status"].validate_output(status.structuredContent)


@pytest.mark.anyio
async def test_mcp_validation_and_application_errors_use_one_typed_envelope(
    forge_env: ForgeEnvironment,
) -> None:
    server = create_server(service=forge_env.service)
    async with create_connected_server_and_client_session(server) as session:
        invalid = await session.call_tool("repo_list", {"undeclared": True})
        assert invalid.isError is True
        invalid_text = "\n".join(getattr(item, "text", "") for item in invalid.content)
        invalid_payload = json.loads(invalid_text)
        assert invalid_payload["status"] == "failed"
        assert invalid_payload["error"]["details"]["correlation_id"]

        missing = await session.call_tool(
            "repo_read",
            {"repo_id": "missing", "files": [{"path": "README.md"}]},
        )
        assert missing.isError is True
        missing_text = "\n".join(getattr(item, "text", "") for item in missing.content)
        missing_payload = json.loads(missing_text)
        assert set(missing_payload) >= {"status", "summary", "error"}
        assert set(missing_payload["error"]) >= {
            "code",
            "message",
            "why",
            "details",
            "safe_next_action",
            "retryable",
        }
        assert missing_payload["error"]["details"]["correlation_id"]
