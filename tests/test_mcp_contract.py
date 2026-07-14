from __future__ import annotations

import pytest
from conftest import ForgeEnvironment
from mcp.shared.memory import create_connected_server_and_client_session

from repoforge.application.service import CodingService
from repoforge.config import load_config
from repoforge.interfaces.mcp.server import create_server, tool_surface_hash


def test_tool_surface_hash_is_deterministic() -> None:
    assert tool_surface_hash() == tool_surface_hash()
    assert len(tool_surface_hash()) == 64


@pytest.mark.anyio
async def test_mcp_protocol_contract_and_annotations(forge_env: ForgeEnvironment) -> None:
    server = create_server(forge_env.config_path)
    async with create_connected_server_and_client_session(server) as session:
        result = await session.list_tools()
        names = {tool.name for tool in result.tools}
        expected = {
            "operation_status",
            "operation_list",
            "operation_cancel",
            "repo_list",
            "repo_status",
            "repo_context",
            "repo_tree",
            "repo_read_file",
            "repo_read_files",
            "repo_search",
            "repo_recent_commits",
            "repo_issue_read",
            "repo_pr_read",
            "workspace_create",
            "workspace_list",
            "workspace_status",
            "workspace_tree",
            "workspace_read_file",
            "workspace_read_files",
            "workspace_search",
            "workspace_write_file",
            "workspace_replace_text",
            "workspace_apply_patch",
            "workspace_restore_paths",
            "workspace_diff",
            "workspace_run_profile",
            "workspace_verify",
            "workspace_commit",
            "workspace_push",
            "workspace_create_draft_pr",
            "workspace_update_draft_pr",
            "workspace_pr_status",
            "workspace_pr_checks",
            "workspace_pr_check_details",
            "workspace_pr_failure_evidence",
            "workspace_remove",
        }
        assert names == expected
        assert "run_shell" not in names
        assert "merge_pull_request" not in names
        assert "force_push" not in names

        for tool in result.tools:
            assert tool.description and tool.description.startswith("Use this")
            assert tool.annotations is not None
            assert tool.annotations.readOnlyHint is not None
            assert tool.annotations.destructiveHint is not None
            assert tool.annotations.openWorldHint is not None
            assert tool.inputSchema["type"] == "object"

        read_result = await session.call_tool("repo_list", {})
        assert read_result.isError is False
        structured = read_result.structuredContent
        assert structured is not None
        assert structured["repositories"][0]["repo_id"] == "demo"

        context_result = await session.call_tool("repo_context", {"repo_id": "demo"})
        assert context_result.isError is False
        assert context_result.structuredContent["package_manager"] == "pnpm@10.20.0"


@pytest.mark.anyio
async def test_mcp_error_is_returned_as_tool_error(forge_env: ForgeEnvironment) -> None:
    server = create_server(forge_env.config_path)
    async with create_connected_server_and_client_session(server) as session:
        result = await session.call_tool("repo_status", {"repo_id": "missing"})
        assert result.isError is True
        rendered = "\n".join(
            item.text for item in result.content if getattr(item, "type", None) == "text"
        )
        assert "Unknown repository id" in rendered


@pytest.mark.anyio
async def test_all_tools_through_mcp_protocol(forge_env: ForgeEnvironment) -> None:
    service = CodingService(load_config(forge_env.config_path))
    operation = service.operations.create(
        kind="contract",
        phase="queued",
        cancel_supported=True,
        task_id="mcp-contract",
    )
    operation = service.operations.start(operation.operation_id)
    server = create_server(service=service)
    async with create_connected_server_and_client_session(server) as session:

        async def call(name: str, arguments: dict[str, object]) -> dict[str, object]:
            result = await session.call_tool(name, arguments)
            assert result.isError is False, (name, result.content)
            assert result.structuredContent is not None
            return result.structuredContent

        await call("operation_status", {"operation_id": operation.operation_id})
        await call(
            "operation_list",
            {"scope": "task:mcp-contract", "state": "running", "limit": 20},
        )
        await call(
            "operation_cancel",
            {
                "operation_id": operation.operation_id,
                "expected_updated_at": operation.updated_at,
            },
        )
        await call("repo_list", {})
        await call("repo_status", {"repo_id": "demo"})
        await call("repo_context", {"repo_id": "demo"})
        snapshot = await call("repo_tree", {"repo_id": "demo", "max_entries": 50})
        assert snapshot["resolved_ref"] == "refs/heads/main"
        await call(
            "repo_read_file",
            {"repo_id": "demo", "relative_path": "hello.txt"},
        )
        await call(
            "repo_read_files",
            {
                "repo_id": "demo",
                "relative_paths": ["hello.txt", "README.md"],
            },
        )
        await call(
            "repo_search",
            {"repo_id": "demo", "query": "Repository", "max_results": 20},
        )
        await call("repo_recent_commits", {"repo_id": "demo", "limit": 2})
        await call("repo_issue_read", {"repo_id": "demo", "issue_number": 1})
        await call("repo_pr_read", {"repo_id": "demo", "pr_number": 2})

        created = await call("workspace_create", {"repo_id": "demo", "task_slug": "MCP contract"})
        workspace_id = str(created["workspace_id"])
        await call("workspace_list", {})
        await call("workspace_status", {"workspace_id": workspace_id})
        await call("workspace_tree", {"workspace_id": workspace_id, "max_entries": 50})
        hello = await call(
            "workspace_read_file",
            {"workspace_id": workspace_id, "relative_path": "hello.txt"},
        )
        await call(
            "workspace_read_files",
            {
                "workspace_id": workspace_id,
                "relative_paths": ["hello.txt", "README.md"],
            },
        )
        await call(
            "workspace_search",
            {"workspace_id": workspace_id, "query": "Repository", "max_results": 20},
        )
        await call(
            "workspace_replace_text",
            {
                "workspace_id": workspace_id,
                "relative_path": "hello.txt",
                "old_text": "hello",
                "new_text": "changed via MCP",
                "expected_sha256": hello["sha256"],
                "expected_occurrences": 1,
            },
        )
        await call(
            "workspace_write_file",
            {
                "workspace_id": workspace_id,
                "relative_path": "scratch.txt",
                "content": "scratch\n",
                "expected_sha256": "<new>",
            },
        )
        restore_status = await call("workspace_status", {"workspace_id": workspace_id})
        await call(
            "workspace_restore_paths",
            {
                "workspace_id": workspace_id,
                "relative_paths": ["scratch.txt"],
                "expected_workspace_fingerprint": restore_status["workspace_fingerprint"],
            },
        )
        patch_status = await call("workspace_status", {"workspace_id": workspace_id})
        await call(
            "workspace_apply_patch",
            {
                "workspace_id": workspace_id,
                "patch": "diff --git a/README.md b/README.md\n--- a/README.md\n+++ b/README.md\n@@ -1,3 +1,4 @@\n # Demo\n \n Repository instructions.\n+MCP tested.\n",
                "expected_head_sha": patch_status["head_sha"],
                "expected_workspace_fingerprint": patch_status["workspace_fingerprint"],
            },
        )
        await call("workspace_diff", {"workspace_id": workspace_id})
        await call(
            "workspace_run_profile",
            {"workspace_id": workspace_id, "profile_name": "quick"},
        )
        await call("workspace_verify", {"workspace_id": workspace_id})
        committed = await call(
            "workspace_commit",
            {"workspace_id": workspace_id, "message": "Exercise every MCP tool"},
        )
        await call("workspace_push", {"workspace_id": workspace_id})
        await call(
            "workspace_create_draft_pr",
            {"workspace_id": workspace_id, "title": "MCP contract", "body": "Test body"},
        )
        await call(
            "workspace_update_draft_pr",
            {"workspace_id": workspace_id, "title": "MCP contract updated"},
        )
        await call("workspace_pr_status", {"workspace_id": workspace_id})
        checks = await call(
            "workspace_pr_checks",
            {"workspace_id": workspace_id, "required_only": True},
        )
        selector = checks["checks"][0]["selector"]
        await call(
            "workspace_pr_check_details",
            {"workspace_id": workspace_id, "check_selector": selector},
        )
        await call(
            "workspace_pr_failure_evidence",
            {
                "workspace_id": workspace_id,
                "check_selector": selector,
                "max_excerpt_lines": 20,
            },
        )
        assert committed["head_sha"]
        await call(
            "workspace_remove",
            {"workspace_id": workspace_id, "delete_local_branch": True},
        )
