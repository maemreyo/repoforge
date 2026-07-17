from __future__ import annotations

import pytest
from conftest import ForgeEnvironment
from mcp.shared.memory import create_connected_server_and_client_session

from repoforge.application.service import CodingService
from repoforge.config import load_config
from repoforge.interfaces.mcp import server as mcp_server
from repoforge.interfaces.mcp.contract import build_release_contract
from repoforge.interfaces.mcp.server import create_server, tool_surface_hash


def test_tool_surface_hash_is_deterministic() -> None:
    assert tool_surface_hash() == tool_surface_hash()
    assert tool_surface_hash(1) != tool_surface_hash(2)
    assert len(tool_surface_hash()) == 64


@pytest.mark.anyio
async def test_release_contract_carries_current_and_legacy_alias_evidence() -> None:
    contract = await build_release_contract()

    assert contract["contract_version"] == 2
    tool_contract = contract["mcp"]["tool_contract"]
    assert tool_contract["current_version"] == 2
    assert tool_contract["supported_versions"] == [1, 2]
    assert [tool["name"] for tool in contract["mcp"]["tools"]].count("workspace_verify") == 0
    legacy_alias = tool_contract["legacy_alias_tools"][0]
    canonical = next(
        tool for tool in contract["mcp"]["tools"] if tool["name"] == "workspace_run_profile"
    )
    assert legacy_alias["name"] == "workspace_verify"
    assert legacy_alias["description"].startswith("Deprecated compatibility alias")
    assert legacy_alias["annotations"] == canonical["annotations"]


def test_tool_surface_hash_does_not_depend_on_ast_unparse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ast

    def fail_unparse(_node: ast.AST) -> str:
        raise AssertionError("ast.unparse is not stable across supported Python minor versions")

    monkeypatch.setattr(ast, "unparse", fail_unparse)
    assert len(tool_surface_hash()) == 64


def test_multiline_tool_descriptions_are_explicit_and_stable() -> None:
    expected = {
        "repo_search": (
            "Use this to locate literal text in an immutable reviewed repository snapshot. Pass\n"
            "context_lines (0-5) to also return that many surrounding lines on each side of a match\n"
            "instead of a follow-up repo_read_file call; context lines are marked with `-` instead of\n"
            "`:` after the path and line number, and still count toward max_results."
        ),
        "repo_issue_read": (
            "Use this when implementation requirements are defined by a GitHub issue. A recent\n"
            "read of the same issue in this session may be served from a short-lived local cache\n"
            "(marked `cache_hit: true`); pass `fresh=true` to force a live read, e.g. before acting\n"
            "on a check or review that must not be stale."
        ),
        "repo_pr_read": (
            "Use this when reviewing an existing pull request, checks, commits, files, or reviews.\n"
            "A recent read of the same pull request in this session may be served from a short-lived\n"
            "local cache (marked `cache_hit: true`); pass `fresh=true` to force a live read before\n"
            "acting on checks or reviews that must not be stale."
        ),
        "repo_task_context": (
            "Use this when starting or resuming a task to assemble repository context, one\n"
            "ticket's specification, workspace status, and recent commits in a single bounded call\n"
            "instead of chaining repo_context, repo_issue_spec, workspace_status, and\n"
            "repo_recent_commits. Pass issue_number and/or workspace_id to include those sections;\n"
            "omitting either yields an explicit null, not an error. A supplied workspace_id must\n"
            "belong to repo_id or the call fails closed. The ticket section reuses the same\n"
            "short-lived local GitHub read cache as repo_issue_spec. Each section is independently\n"
            "bounded and reports its own `truncated` flag, and the whole bundle is capped at 96 KB,\n"
            "truncating recent_commits first, then ticket, then workspace, then repository last."
        ),
        "workspace_search": (
            "Use this when locating literal text in allowed workspace files; it is not a shell tool.\n"
            "Pass context_lines (0-5) to also return that many surrounding lines on each side of a\n"
            "match instead of a follow-up workspace_read_file call; context lines are marked with `-`\n"
            "instead of `:` after the path and line number, and still count toward max_results."
        ),
    }

    assert getattr(mcp_server, "_MULTILINE_TOOL_DESCRIPTIONS", None) == expected


@pytest.mark.anyio
async def test_mcp_protocol_contract_and_annotations(
    forge_env: ForgeEnvironment,
) -> None:
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
            "repo_commit_read",
            "repo_compare",
            "repo_issue_read",
            "repo_issue_graph",
            "repo_issue_next",
            "repo_issue_spec",
            "repo_pr_read",
            "repo_task_context",
            "workspace_create",
            "workspace_list",
            "workspace_status",
            "workspace_base_status",
            "workspace_refresh_preview",
            "workspace_refresh",
            "workspace_tree",
            "workspace_read_file",
            "workspace_read_files",
            "workspace_search",
            "workspace_write_file",
            "workspace_edit",
            "workspace_apply_patch",
            "workspace_restore_paths",
            "workspace_diff",
            "workspace_run_profile",
            "workspace_run_diagnostic",
            "workspace_hygiene_status",
            "workspace_format_changed",
            "workspace_run_adhoc",
            "workspace_commit",
            "workspace_push",
            "workspace_create_draft_pr",
            "workspace_update_draft_pr",
            "workspace_pr_status",
            "workspace_pr_checks",
            "workspace_pr_check_details",
            "workspace_pr_failure_evidence",
            "workspace_pr_watch",
            "workspace_remove",
            "config_inspect",
            "runtime_logs_read",
            "repo_policy_apply",
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
        for name in ("repo_commit_read", "repo_compare"):
            evidence_tool = next(tool for tool in result.tools if tool.name == name)
            assert evidence_tool.annotations.readOnlyHint is True
            assert evidence_tool.annotations.destructiveHint is False
            assert evidence_tool.annotations.openWorldHint is False

        tools = {tool.name: tool for tool in result.tools}
        workspace_edit_schema = tools["workspace_edit"].inputSchema
        file_item = workspace_edit_schema["properties"]["files"]["items"]
        assert file_item["type"] == "object"
        assert set(file_item["required"]) == {"path", "expected_sha256", "edits"}
        edit_item = file_item["properties"]["edits"]["items"]
        assert edit_item["type"] == "object"
        assert set(edit_item["required"]) == {"old_text", "new_text"}
        for mutation_name in (
            "workspace_write_file",
            "workspace_edit",
            "workspace_apply_patch",
        ):
            mutation = tools[mutation_name]
            assert "idempotency_key" in mutation.inputSchema["properties"]
            assert "idempotency_key" not in mutation.inputSchema.get("required", [])
            assert "idempotency key" in mutation.description.lower()
        for verification_name in (
            "workspace_run_profile",
            "workspace_run_diagnostic",
        ):
            verification = tools[verification_name]
            assert "force_rerun" in verification.inputSchema["properties"]
            assert "force_rerun" not in verification.inputSchema.get("required", [])
            assert "force_rerun" in verification.description
        for search_name in ("repo_search", "workspace_search"):
            search_schema = tools[search_name].inputSchema["properties"]
            assert search_schema["context_lines"]["minimum"] == 0
            assert search_schema["context_lines"]["maximum"] == 5
            assert search_schema["max_results"]["minimum"] == 1
            assert search_schema["max_results"]["maximum"] == 200
        for graph_name in ("repo_issue_graph", "repo_issue_next"):
            fresh_schema = tools[graph_name].inputSchema["properties"]["fresh"]
            assert fresh_schema["type"] == "boolean"
            assert fresh_schema["default"] is False

        run_profile = tools["workspace_run_profile"]
        assert "profile_name" not in run_profile.inputSchema.get("required", [])
        assert set(run_profile.inputSchema["properties"]) == {
            "workspace_id",
            "profile_name",
            "background",
            "force_rerun",
        }
        assert tools["workspace_create"].description == (
            "Use this before editing to create an isolated ai/* worktree; use an idempotency key for\n"
            "retries. Create one workspace per issue; pass issue_ids only when several dependent\n"
            "(stacked) issues are deliberately worked in this same workspace. issue_ids is\n"
            "display-only metadata, not validated against any tracker."
        )
        assert tools["workspace_list"].description == (
            "Use this when resuming work or finding active RepoForge workspaces; each entry reports age,\n"
            "dirty state, and linked issue_ids to help decide what to reuse or remove."
        )
        diagnostic = tools["workspace_run_diagnostic"]
        assert diagnostic.annotations.readOnlyHint is False
        assert diagnostic.annotations.destructiveHint is False
        assert diagnostic.annotations.openWorldHint is False
        assert set(diagnostic.inputSchema["properties"]) == {
            "workspace_id",
            "diagnostic_id",
            "selector",
            "expected_fingerprint",
            "intent",
            "expectation",
            "expected_failure_class",
            "selector2",
            "force_rerun",
        }
        hygiene = tools["workspace_hygiene_status"]
        assert hygiene.annotations.readOnlyHint is True
        assert hygiene.annotations.destructiveHint is False
        assert hygiene.annotations.openWorldHint is False
        assert set(hygiene.inputSchema["properties"]) == {
            "workspace_id",
            "formatter_id",
        }
        formatter = tools["workspace_format_changed"]
        assert formatter.annotations.readOnlyHint is False
        assert formatter.annotations.destructiveHint is False
        assert formatter.annotations.openWorldHint is False
        assert set(formatter.inputSchema["properties"]) == {
            "workspace_id",
            "expected_fingerprint",
            "formatter_id",
        }
        assert set(formatter.inputSchema["required"]) == {
            "workspace_id",
            "expected_fingerprint",
        }
        assert "paths" not in formatter.inputSchema["properties"]
        assert "argv" not in formatter.inputSchema["properties"]
        for name in ("workspace_base_status", "workspace_refresh_preview"):
            annotations = tools[name].annotations
            assert annotations is not None
            assert annotations.readOnlyHint is True
            assert annotations.destructiveHint is False
            assert annotations.idempotentHint is True
            assert annotations.openWorldHint is True
        refresh_annotations = tools["workspace_refresh"].annotations
        assert refresh_annotations is not None
        assert refresh_annotations.readOnlyHint is False
        assert refresh_annotations.destructiveHint is False
        assert refresh_annotations.idempotentHint is False
        assert refresh_annotations.openWorldHint is True

        read_result = await session.call_tool("repo_list", {})
        assert read_result.isError is False
        structured = read_result.structuredContent
        assert structured is not None
        assert structured["repositories"][0]["repo_id"] == "demo"

        context_result = await session.call_tool("repo_context", {"repo_id": "demo"})
        assert context_result.isError is False
        assert context_result.structuredContent["package_manager"] == "pnpm@10.20.0"


@pytest.mark.anyio
async def test_versioned_verify_alias_window_routes_to_canonical_profile(
    forge_env: ForgeEnvironment,
) -> None:
    current_server = create_server(forge_env.config_path, contract_version=2)
    async with create_connected_server_and_client_session(current_server) as session:
        current_tools = {tool.name for tool in (await session.list_tools()).tools}
        assert "workspace_verify" not in current_tools
        rejected = await session.call_tool("workspace_verify", {"workspace_id": "missing"})
        assert rejected.isError is True
        rendered = "\n".join(
            item.text for item in rejected.content if getattr(item, "type", None) == "text"
        )
        assert "tool contract v2" in rendered

    legacy_server = create_server(forge_env.config_path, contract_version=1)
    async with create_connected_server_and_client_session(legacy_server) as session:
        legacy_tools = {tool.name: tool for tool in (await session.list_tools()).tools}
        alias_tool = legacy_tools["workspace_verify"]
        canonical_tool = legacy_tools["workspace_run_profile"]
        assert "Deprecated compatibility alias" in alias_tool.description
        assert alias_tool.annotations == canonical_tool.annotations

        canonical_workspace = await session.call_tool(
            "workspace_create",
            {"repo_id": "demo", "task_slug": "canonical contract verification"},
        )
        alias_workspace = await session.call_tool(
            "workspace_create",
            {"repo_id": "demo", "task_slug": "legacy contract verification"},
        )
        assert canonical_workspace.structuredContent is not None
        assert alias_workspace.structuredContent is not None

        for created in (canonical_workspace, alias_workspace):
            workspace_id = created.structuredContent["workspace_id"]
            current = await session.call_tool(
                "workspace_read_file",
                {"workspace_id": workspace_id, "relative_path": "hello.txt"},
            )
            assert current.isError is False
            assert current.structuredContent is not None
            prepared = await session.call_tool(
                "workspace_write_file",
                {
                    "workspace_id": workspace_id,
                    "relative_path": "hello.txt",
                    "content": "changed for verification parity\n",
                    "expected_sha256": current.structuredContent["sha256"],
                },
            )
            assert prepared.isError is False

        canonical = await session.call_tool(
            "workspace_run_profile",
            {"workspace_id": canonical_workspace.structuredContent["workspace_id"]},
        )
        alias = await session.call_tool(
            "workspace_verify",
            {"workspace_id": alias_workspace.structuredContent["workspace_id"]},
        )
        assert canonical.isError is False
        assert alias.isError is False
        assert canonical.structuredContent is not None
        assert alias.structuredContent is not None
        for key in (
            "profile",
            "description",
            "verification",
            "satisfies_commit_gate",
            "used_default",
            "repo_id",
            "working_directory",
        ):
            assert alias.structuredContent[key] == canonical.structuredContent[key]
        alias_commands = alias.structuredContent["commands"]
        canonical_commands = canonical.structuredContent["commands"]
        assert len(alias_commands) == len(canonical_commands)
        for alias_command, canonical_command in zip(
            alias_commands, canonical_commands, strict=True
        ):
            assert alias_command["duration_ms"] >= 0
            assert canonical_command["duration_ms"] >= 0
            assert alias_command["cumulative_duration_ms"] >= alias_command["duration_ms"]
            assert canonical_command["cumulative_duration_ms"] >= canonical_command["duration_ms"]
            assert {
                key: value
                for key, value in alias_command.items()
                if key not in {"duration_ms", "cumulative_duration_ms"}
            } == {
                key: value
                for key, value in canonical_command.items()
                if key not in {"duration_ms", "cumulative_duration_ms"}
            }


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
        contextual_repo_search = await call(
            "repo_search",
            {
                "repo_id": "demo",
                "query": "Repository",
                "max_results": 20,
                "context_lines": 1,
            },
        )
        assert contextual_repo_search["matches"] == [
            "README.md-2-",
            "README.md:3:Repository instructions.",
        ]
        await call("repo_recent_commits", {"repo_id": "demo", "limit": 2})
        commit = await call(
            "repo_commit_read",
            {"repo_id": "demo", "ref": "main", "max_files": 20},
        )
        assert commit["commit_sha"] == snapshot["commit_sha"]
        comparison = await call(
            "repo_compare",
            {
                "repo_id": "demo",
                "base_ref": "main",
                "head_ref": "main",
                "max_files": 20,
            },
        )
        assert comparison["merge_base_sha"] == snapshot["commit_sha"]
        assert comparison["total_files"] == 0
        await call("repo_issue_read", {"repo_id": "demo", "issue_number": 1})
        graph_result = await call("repo_issue_graph", {"repo_id": "demo"})
        assert graph_result["source"] == "github"
        assert graph_result["evidence_complete"] is False
        assert graph_result["node_count"] == 0
        next_result = await call("repo_issue_next", {"repo_id": "demo"})
        assert next_result["source"] == "github"
        assert next_result["valid"] is False
        assert next_result["diagnostics"][0]["code"] == "GRAPH_NOT_CONFIGURED"
        spec_result = await call("repo_issue_spec", {"repo_id": "demo", "issue_number": 1})
        assert spec_result["source"] == "github"
        assert spec_result["graph_member"] is False
        assert spec_result["live"]["title"] == "Implement safer workflow"
        await call("repo_pr_read", {"repo_id": "demo", "pr_number": 2})

        created = await call(
            "workspace_create",
            {
                "repo_id": "demo",
                "task_slug": "MCP contract",
                "issue_ids": ["42", "#43"],
            },
        )
        assert created["issue_ids"] == ["42", "#43"]
        workspace_id = str(created["workspace_id"])
        listed = await call("workspace_list", {})
        listed_entry = next(
            item for item in listed["workspaces"] if item["workspace_id"] == workspace_id
        )
        assert listed_entry["issue_ids"] == ["42", "#43"]
        assert listed_entry["dirty"] is False
        diagnostic_status = await call("workspace_status", {"workspace_id": workspace_id})
        base_status = await call("workspace_base_status", {"workspace_id": workspace_id})
        assert base_status["staleness"] == "current"
        refresh_preview = await call(
            "workspace_refresh_preview",
            {
                "workspace_id": workspace_id,
                "expected_head_sha": diagnostic_status["head_sha"],
                "expected_fingerprint": diagnostic_status["workspace_fingerprint"],
            },
        )
        refresh = await call(
            "workspace_refresh",
            {
                "workspace_id": workspace_id,
                "preview_id": refresh_preview["preview_id"],
                "expected_head_sha": diagnostic_status["head_sha"],
                "expected_fingerprint": diagnostic_status["workspace_fingerprint"],
            },
        )
        assert refresh["status"] == "current"
        diagnostic_result = await call(
            "workspace_run_diagnostic",
            {
                "workspace_id": workspace_id,
                "diagnostic_id": "pytest-target",
                "selector": "hello.txt::test_example",
                "expected_fingerprint": diagnostic_status["workspace_fingerprint"],
                "intent": "tdd_green",
                "expectation": "pass",
            },
        )
        assert diagnostic_result["outcome"] == "passed"
        assert diagnostic_result["resolved_selector"] == "hello.txt::test_example"
        assert diagnostic_result["intent"] == "tdd_green"
        assert diagnostic_result["expectation_met"] is True
        assert diagnostic_result["business_tests_ran"] is True
        assert diagnostic_result["valid_tdd_red_evidence"] is False
        hygiene = await call(
            "workspace_hygiene_status",
            {"workspace_id": workspace_id},
        )
        assert hygiene["status"] == "available"
        assert hygiene["formatter_id"] == "test-format"

        # This fixture's "demo" repository is enrolled strict (the default); exercise
        # workspace_run_adhoc's structured refusal path through the protocol boundary.
        adhoc_refusal = await session.call_tool(
            "workspace_run_adhoc",
            {"workspace_id": workspace_id, "argv": ["python3", "--version"]},
        )
        assert adhoc_refusal.isError is True
        adhoc_refusal_text = "\n".join(
            item.text for item in adhoc_refusal.content if getattr(item, "type", None) == "text"
        )
        assert "EXECUTION_MODE_STRICT" in adhoc_refusal_text
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
        contextual_workspace_search = await call(
            "workspace_search",
            {
                "workspace_id": workspace_id,
                "query": "Repository",
                "max_results": 20,
                "context_lines": 1,
            },
        )
        assert contextual_workspace_search["matches"] == [
            "README.md-2-",
            "README.md:3:Repository instructions.",
        ]
        await call(
            "workspace_edit",
            {
                "workspace_id": workspace_id,
                "files": [
                    {
                        "path": "hello.txt",
                        "expected_sha256": hello["sha256"],
                        "edits": [{"old_text": "hello", "new_text": "changed via MCP"}],
                    }
                ],
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
        format_status = await call("workspace_status", {"workspace_id": workspace_id})
        formatted = await call(
            "workspace_format_changed",
            {
                "workspace_id": workspace_id,
                "expected_fingerprint": format_status["workspace_fingerprint"],
            },
        )
        assert formatted["selected_paths"] == ["hello.txt"]
        assert formatted["fingerprint_changed"] is False
        await call(
            "workspace_run_profile",
            {"workspace_id": workspace_id, "profile_name": "quick"},
        )
        await call("workspace_run_profile", {"workspace_id": workspace_id})
        committed = await call(
            "workspace_commit",
            {"workspace_id": workspace_id, "message": "Exercise every MCP tool"},
        )
        await call("workspace_push", {"workspace_id": workspace_id})
        await call(
            "workspace_create_draft_pr",
            {
                "workspace_id": workspace_id,
                "title": "MCP contract",
                "body": "Test body",
            },
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


@pytest.mark.anyio
async def test_workspace_edit_batch_edits_through_mcp_protocol(
    forge_env: ForgeEnvironment,
) -> None:
    """`workspace_edit`'s `files`/`edits` shape applies several ordered replacements,
    across one or more files, in one call.

    Regression coverage for issue #142 (multi-file generalization): one `workspace_edit`
    call with a bounded `files` list, each carrying a bounded `edits` list, must apply
    every entry atomically under one lock/fingerprint cycle, and a failing entry anywhere
    must reject the whole call without touching any file.
    """
    server = create_server(forge_env.config_path)
    async with create_connected_server_and_client_session(server) as session:

        async def call(name: str, arguments: dict[str, object]) -> dict[str, object]:
            result = await session.call_tool(name, arguments)
            assert result.isError is False, (name, result.content)
            assert result.structuredContent is not None
            return result.structuredContent

        created = await call("workspace_create", {"repo_id": "demo", "task_slug": "batch edit"})
        workspace_id = str(created["workspace_id"])
        hello = await call(
            "workspace_read_file",
            {"workspace_id": workspace_id, "relative_path": "hello.txt"},
        )

        batch_result = await call(
            "workspace_edit",
            {
                "workspace_id": workspace_id,
                "files": [
                    {
                        "path": "hello.txt",
                        "expected_sha256": hello["sha256"],
                        "edits": [
                            {"old_text": "hello", "new_text": "hi there"},
                            {"old_text": "hi there", "new_text": "hi there, batched"},
                        ],
                    }
                ],
            },
        )
        assert batch_result["files"] == [
            {
                "path": "hello.txt",
                "sha256": batch_result["files"][0]["sha256"],
                "replacements": 2,
            }
        ]

        status_before_failure = await call("workspace_status", {"workspace_id": workspace_id})
        failing = await session.call_tool(
            "workspace_edit",
            {
                "workspace_id": workspace_id,
                "files": [
                    {
                        "path": "hello.txt",
                        "expected_sha256": batch_result["files"][0]["sha256"],
                        "edits": [
                            {
                                "old_text": "hi there, batched",
                                "new_text": "changed once more",
                            },
                            {
                                "old_text": "text that does not exist",
                                "new_text": "unreachable",
                            },
                        ],
                    }
                ],
            },
        )
        assert failing.isError is True
        rendered = "\n".join(
            item.text for item in failing.content if getattr(item, "type", None) == "text"
        )
        assert "edits[1]" in rendered
        status_after_failure = await call("workspace_status", {"workspace_id": workspace_id})
        assert (
            status_after_failure["workspace_fingerprint"]
            == status_before_failure["workspace_fingerprint"]
        )
        assert status_after_failure["head_sha"] == status_before_failure["head_sha"]

        too_many_edits = await session.call_tool(
            "workspace_edit",
            {
                "workspace_id": workspace_id,
                "files": [
                    {
                        "path": "hello.txt",
                        "expected_sha256": batch_result["files"][0]["sha256"],
                        "edits": [{"old_text": "hi", "new_text": "hi"} for _ in range(21)],
                    }
                ],
            },
        )
        assert too_many_edits.isError is True
        bound_rendered = "\n".join(
            item.text for item in too_many_edits.content if getattr(item, "type", None) == "text"
        )
        assert "at most 20 entries, got 21" in bound_rendered
