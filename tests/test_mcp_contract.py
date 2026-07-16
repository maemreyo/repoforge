from __future__ import annotations

import pytest
from conftest import ForgeEnvironment
from mcp.shared.memory import create_connected_server_and_client_session

from repoforge.application.service import CodingService
from repoforge.config import load_config
from repoforge.interfaces.mcp import server as mcp_server
from repoforge.interfaces.mcp.server import create_server, tool_surface_hash


def test_tool_surface_hash_is_deterministic() -> None:
    assert tool_surface_hash() == tool_surface_hash()
    assert len(tool_surface_hash()) == 64


def test_tool_surface_hash_does_not_depend_on_ast_unparse(monkeypatch: pytest.MonkeyPatch) -> None:
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
            "workspace_replace_text",
            "workspace_apply_patch",
            "workspace_restore_paths",
            "workspace_diff",
            "workspace_run_profile",
            "workspace_run_diagnostic",
            "workspace_hygiene_status",
            "workspace_format_changed",
            "workspace_verify",
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
        assert graph_result["manifest_found"] is False
        next_result = await call("repo_issue_next", {"repo_id": "demo"})
        assert next_result["manifest_found"] is False
        spec_result = await call("repo_issue_spec", {"repo_id": "demo", "issue_number": 1})
        assert spec_result["manifest_found"] is False
        assert spec_result["live"]["title"] == "Implement safer workflow"
        await call("repo_pr_read", {"repo_id": "demo", "pr_number": 2})

        created = await call(
            "workspace_create",
            {"repo_id": "demo", "task_slug": "MCP contract", "issue_ids": ["42", "#43"]},
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


@pytest.mark.anyio
async def test_workspace_replace_text_batch_edits_through_mcp_protocol(
    forge_env: ForgeEnvironment,
) -> None:
    """The additive `edits` parameter applies several ordered replacements in one call.

    Regression coverage for issue #142: one `workspace_replace_text` call with a bounded
    `edits` list must apply every entry atomically under one lock/fingerprint cycle, and a
    failing entry must reject the whole call by index without touching the file.
    """
    server = create_server(forge_env.config_path)
    async with create_connected_server_and_client_session(server) as session:

        async def call(name: str, arguments: dict[str, object]) -> dict[str, object]:
            result = await session.call_tool(name, arguments)
            assert result.isError is False, (name, result.content)
            assert result.structuredContent is not None
            return result.structuredContent

        created = await call(
            "workspace_create", {"repo_id": "demo", "task_slug": "batch replace text"}
        )
        workspace_id = str(created["workspace_id"])
        hello = await call(
            "workspace_read_file",
            {"workspace_id": workspace_id, "relative_path": "hello.txt"},
        )

        batch_result = await call(
            "workspace_replace_text",
            {
                "workspace_id": workspace_id,
                "relative_path": "hello.txt",
                "expected_sha256": hello["sha256"],
                "edits": [
                    {"old_text": "hello", "new_text": "hi there"},
                    {"old_text": "hi there", "new_text": "hi there, batched"},
                ],
            },
        )
        assert batch_result["replacements"] == 2
        assert batch_result["edits"] == [
            {"index": 0, "replacements": 1},
            {"index": 1, "replacements": 1},
        ]

        status_before_failure = await call("workspace_status", {"workspace_id": workspace_id})
        failing = await session.call_tool(
            "workspace_replace_text",
            {
                "workspace_id": workspace_id,
                "relative_path": "hello.txt",
                "expected_sha256": batch_result["sha256"],
                "edits": [
                    {"old_text": "hi there, batched", "new_text": "changed once more"},
                    {"old_text": "text that does not exist", "new_text": "unreachable"},
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
            "workspace_replace_text",
            {
                "workspace_id": workspace_id,
                "relative_path": "hello.txt",
                "expected_sha256": batch_result["sha256"],
                "edits": [{"old_text": "hi", "new_text": "hi"} for _ in range(21)],
            },
        )
        assert too_many_edits.isError is True
        bound_rendered = "\n".join(
            item.text for item in too_many_edits.content if getattr(item, "type", None) == "text"
        )
        assert "at most 20 entries, got 21" in bound_rendered
