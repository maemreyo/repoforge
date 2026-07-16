"""Thin MCP interface: parse typed inputs, call CodingService, return stable dictionaries."""

from __future__ import annotations

import ast
import hashlib
import inspect
import json
import os
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import Tool as McpTool
from mcp.types import ToolAnnotations

from ...application.runtime.hot_reload import AtomicServiceRouter
from ...application.service import CodingService
from ...application.workspace.edit import FileEdit
from ...config import load_config
from ...domain.errors import operation_error_from_exception
from ...domain.operations import automatic_retry_allowed
from ...domain.redaction import redact_text
from ...domain.tool_contract import ToolContractRegistry, default_tool_contract_registry
from .capabilities import client_capabilities_from_context


class ContractAwareFastMCP(FastMCP[None]):
    """Expose one request-scoped reviewed tool contract without mutating registration."""

    def __init__(
        self,
        *args: Any,
        contract_registry: ToolContractRegistry,
        contract_version: int | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._contract_registry = contract_registry
        self._contract_version = contract_version
        if contract_version is not None:
            self._contract_registry.tool_names(contract_version, frozenset())

    def _selected_contract_version(self) -> int:
        if self._contract_version is not None:
            return self._contract_version
        try:
            capabilities = client_capabilities_from_context(self.get_context())
        except (LookupError, ValueError, AttributeError):
            return self._contract_registry.current_version
        return self._contract_registry.resolve(capabilities).version

    async def list_tools(self) -> list[McpTool]:
        tools = await super().list_tools()
        version = self._selected_contract_version()
        allowed = self._contract_registry.tool_names(
            version,
            frozenset(tool.name for tool in tools),
        )
        aliases = {
            alias.alias: alias
            for alias in self._contract_registry.aliases
            if alias.active_in(version)
        }
        visible: list[McpTool] = []
        for tool in tools:
            if tool.name not in allowed:
                continue
            alias = aliases.get(tool.name)
            if alias is None:
                visible.append(tool)
                continue
            description = f"{alias.notice} {tool.description or ''}".strip()
            visible.append(tool.model_copy(update={"description": description}))
        return visible

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        all_tools = await super().list_tools()
        registered = frozenset(tool.name for tool in all_tools)
        version = self._selected_contract_version()
        allowed = self._contract_registry.tool_names(version, registered)
        if name in registered and name not in allowed:
            raise ValueError(
                f"Tool {name!r} is not available in RepoForge tool contract v{version}"
            )
        return await super().call_tool(name, arguments)


class _StructuredMcpToolError(RuntimeError):
    """Signal a stable structured failure while preserving MCP isError semantics."""


class _ServiceErrorBoundary:
    """Convert known application failures into the stable structured MCP envelope."""

    def __init__(
        self,
        service: Any | None = None,
        *,
        router: AtomicServiceRouter | None = None,
    ) -> None:
        if (service is None) == (router is None):
            raise ValueError("Exactly one of service or router must be provided")
        self._service = service
        self._router = router

    @contextmanager
    def _selected_service(self) -> Iterator[Any]:
        if self._router is None:
            yield self._service
            return
        with self._router.acquire() as container:
            yield container.service

    def call(self, name: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
        has_idempotency_key = False
        try:
            with self._selected_service() as service:
                target = getattr(service, name)
                bound = inspect.signature(target).bind_partial(*args, **kwargs)
                has_idempotency_key = bool(bound.arguments.get("idempotency_key"))
                result = target(*args, **kwargs)
            if not isinstance(result, dict):
                raise TypeError("MCP service operation must return an object")
            return result
        except Exception as exc:
            envelope = operation_error_from_exception(exc)
            correlation_id = envelope.correlation_id or secrets.token_hex(12)
            payload = {
                "status": "failed",
                "error_code": envelope.code.value,
                "what_happened": redact_text(
                    envelope.what_happened,
                    secrets=(os.environ.get("CONTROL_PLANE_API_KEY", ""),),
                ),
                "why": envelope.why,
                "correlation_id": correlation_id,
                "unchanged_state": list(envelope.unchanged_state)
                or ["No unreported state transition was committed."],
                "safe_next_action": envelope.safe_next_action,
                "retryable": envelope.retryable,
                "details": envelope.details,
                "automatic_retry_allowed": automatic_retry_allowed(
                    name,
                    envelope.code,
                    has_idempotency_key=has_idempotency_key,
                ),
            }
            raise _StructuredMcpToolError(
                json.dumps(payload, sort_keys=True, ensure_ascii=False)
            ) from exc


SERVER_INSTRUCTIONS = "RepoForge connects ChatGPT to allowlisted local Git repositories through isolated worktrees.\nAlways begin with repo_list, then open a session with repo_task_context (pass issue_number and/or an\nexisting workspace_id when known) to gather bounded repository, ticket, workspace, and recent-commit\ncontext in one call before creating a workspace. Inspect before editing.\nDefault to one issue per workspace_create call; pass every issue_id at creation time only when a\ndeliberate chain of dependent (stacked) issues must be worked sequentially in the same worktree.\nissue_ids cannot be changed after creation. Prefer exact text replacement or a small validated patch.\nReview workspace_diff after every meaningful change. While iterating on edits, check work with the\nquick profile or workspace_run_diagnostic; they are cheap and meant for the edit-test loop. Reserve the\nfull verification profile for one workspace_run_profile call immediately before commit; omit\nprofile_name to use the repository default. Never claim verification succeeded unless the tool returned\nsuccess. Commit, push, and create only draft pull requests. Never merge, force-push, modify protected\nbranches, request secrets, or bypass path/change-budget policies. Use workspace_restore_paths to safely\nundo selected uncommitted mistakes after refreshing status. Use workspace_list to review workspace age,\ndirty state, and issue_ids before removing or reusing a workspace.".strip()
READ_ONLY = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
)
EXTERNAL_READ = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True
)
EXTERNAL_MUTATE = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True
)
LOCAL_CREATE = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False
)
LOCAL_MUTATE = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False
)
LOCAL_IDEMPOTENT_MUTATE = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False
)
LOCAL_DESTRUCTIVE = ToolAnnotations(
    readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False
)
EXTERNAL_WRITE = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True
)

_WORKSPACE_CREATE_DESCRIPTION = (
    "Use this before editing to create an isolated ai/* worktree; use an idempotency key for\n"
    "retries. Create one workspace per issue; pass issue_ids only when several dependent\n"
    "(stacked) issues are deliberately worked in this same workspace. issue_ids is\n"
    "display-only metadata, not validated against any tracker."
)
_WORKSPACE_LIST_DESCRIPTION = (
    "Use this when resuming work or finding active RepoForge workspaces; each entry reports age,\n"
    "dirty state, and linked issue_ids to help decide what to reuse or remove."
)
_MULTILINE_TOOL_DESCRIPTIONS = {
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


def _canonical_ast_value(value: object) -> object:
    """Serialize selected AST nodes without Python-minor-specific pretty-printing."""

    if isinstance(value, ast.AST):
        return {
            "node": type(value).__name__,
            "fields": {
                name: _canonical_ast_value(field_value)
                for name, field_value in sorted(ast.iter_fields(value))
            },
        }
    if isinstance(value, list):
        return [_canonical_ast_value(item) for item in value]
    if isinstance(value, tuple):
        return [_canonical_ast_value(item) for item in value]
    return value


def tool_surface_hash(contract_version: int | None = None) -> str:
    module = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    create = next(
        n for n in module.body if isinstance(n, ast.FunctionDef) and n.name == "create_server"
    )
    tools = []
    for node in create.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        decorator = next(
            (
                d
                for d in node.decorator_list
                if isinstance(d, ast.Call)
                and isinstance(d.func, ast.Attribute)
                and (d.func.attr == "tool")
            ),
            None,
        )
        if decorator is None:
            continue
        keywords = {
            keyword.arg: _canonical_ast_value(keyword.value)
            for keyword in decorator.keywords
            if keyword.arg is not None
        }
        tools.append(
            {
                "name": node.name,
                "arguments": _canonical_ast_value(node.args),
                "returns": _canonical_ast_value(node.returns),
                "title": keywords.get("title"),
                "annotations": keywords.get("annotations"),
                "structured_output": keywords.get("structured_output"),
            }
        )
    registry = default_tool_contract_registry()
    version = contract_version or registry.current_version
    allowed = registry.tool_names(version, frozenset(str(tool["name"]) for tool in tools))
    versioned_tools = [tool for tool in tools if str(tool["name"]) in allowed]
    return hashlib.sha256(
        json.dumps(
            {
                "schema_version": 2,
                "contract_version": version,
                "tools": sorted(versioned_tools, key=lambda item: item["name"]),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def create_server(
    config_path: str | Path | None = None,
    *,
    service: CodingService | None = None,
    router: AtomicServiceRouter | None = None,
    contract_version: int | None = None,
) -> FastMCP:
    if service is not None and router is not None:
        raise ValueError("create_server accepts either service or router, not both")
    raw_service = service or (
        None if router is not None else CodingService(load_config(config_path))
    )
    bounded_service = _ServiceErrorBoundary(raw_service, router=router)
    mcp = ContractAwareFastMCP(
        "RepoForge",
        instructions=SERVER_INSTRUCTIONS,
        log_level="WARNING",
        contract_registry=default_tool_contract_registry(),
        contract_version=contract_version,
    )

    @mcp.tool(title="Read durable operation status", annotations=READ_ONLY, structured_output=True)
    def operation_status(operation_id: str) -> dict[str, Any]:
        """Use this to inspect one exact durable operation and its bounded progress metadata."""
        return bounded_service.call("operation_status", operation_id)

    @mcp.tool(title="List durable operations", annotations=READ_ONLY, structured_output=True)
    def operation_list(
        scope: str | None = None,
        state: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """Use this to list bounded durable operations by optional task/workspace scope and state."""
        return bounded_service.call("operation_list", scope, state, limit, cursor)

    @mcp.tool(
        title="Request operation cancellation",
        annotations=LOCAL_IDEMPOTENT_MUTATE,
        structured_output=True,
    )
    def operation_cancel(
        operation_id: str,
        expected_updated_at: str | None = None,
    ) -> dict[str, Any]:
        """Use this to idempotently request cancellation without marking terminal cancellation."""
        return bounded_service.call("operation_cancel", operation_id, expected_updated_at)

    @mcp.tool(
        title="List configured repositories",
        annotations=READ_ONLY,
        structured_output=True,
    )
    def repo_list() -> dict[str, Any]:
        """Use this when choosing a repository or discovering its profiles and safety policy."""
        return bounded_service.call(
            "repo_list",
        )

    @mcp.tool(
        title="Inspect repository status",
        annotations=EXTERNAL_READ,
        structured_output=True,
    )
    def repo_status(repo_id: str) -> dict[str, Any]:
        """Use this when checking the source clone, remotes, branch state, and gh authentication."""
        return bounded_service.call("repo_status", repo_id)

    @mcp.tool(title="Read repository context", annotations=READ_ONLY, structured_output=True)
    def repo_context(repo_id: str) -> dict[str, Any]:
        """Use this before planning to inspect manifests, scripts, root files, and instruction previews."""
        return bounded_service.call("repo_context", repo_id)

    @mcp.tool(title="Read committed change evidence", annotations=READ_ONLY, structured_output=True)
    def repo_commit_read(
        repo_id: str,
        ref: str,
        max_files: int = 100,
        include_patch: bool = False,
    ) -> dict[str, Any]:
        """Use this to inspect one exact reviewed commit with bounded file statistics and optional patch evidence."""
        return bounded_service.call("repo_commit_read", repo_id, ref, max_files, include_patch)

    @mcp.tool(
        title="Compare committed repository refs", annotations=READ_ONLY, structured_output=True
    )
    def repo_compare(
        repo_id: str,
        base_ref: str,
        head_ref: str,
        path_glob: str | None = None,
        max_files: int = 100,
        include_patch: bool = False,
    ) -> dict[str, Any]:
        """Use this to compare two exact reviewed commits with merge-base, divergence, bounded files, and optional patch evidence."""
        return bounded_service.call(
            "repo_compare",
            repo_id,
            base_ref,
            head_ref,
            path_glob,
            max_files,
            include_patch,
        )

    @mcp.tool(
        title="List committed repository files", annotations=READ_ONLY, structured_output=True
    )
    def repo_tree(
        repo_id: str,
        ref: str | None = None,
        max_entries: int = 2000,
    ) -> dict[str, Any]:
        """Use this to list files from an immutable reviewed repository snapshot without a workspace."""
        return bounded_service.call("repo_tree", repo_id, ref, max_entries)

    @mcp.tool(title="Read committed repository file", annotations=READ_ONLY, structured_output=True)
    def repo_read_file(
        repo_id: str,
        relative_path: str,
        ref: str | None = None,
        start_line: int = 1,
        end_line: int = 500,
    ) -> dict[str, Any]:
        """Use this to read one UTF-8 file from an immutable reviewed repository snapshot."""
        return bounded_service.call(
            "repo_read_file", repo_id, relative_path, ref, start_line, end_line
        )

    @mcp.tool(
        title="Read multiple committed repository files",
        annotations=READ_ONLY,
        structured_output=True,
    )
    def repo_read_files(
        repo_id: str,
        relative_paths: list[str],
        ref: str | None = None,
        start_line: int = 1,
        end_line: int = 500,
    ) -> dict[str, Any]:
        """Use this to read the same bounded line range from several files in one immutable snapshot."""
        return bounded_service.call(
            "repo_read_files", repo_id, relative_paths, ref, start_line, end_line
        )

    @mcp.tool(
        title="Search committed repository code",
        description=_MULTILINE_TOOL_DESCRIPTIONS["repo_search"],
        annotations=READ_ONLY,
        structured_output=True,
    )
    def repo_search(
        repo_id: str,
        query: str,
        ref: str | None = None,
        path_glob: str | None = None,
        max_results: int = 200,
        context_lines: int = 0,
    ) -> dict[str, Any]:
        """Use this to locate literal text in an immutable reviewed repository snapshot. Pass
        context_lines (0-5) to also return that many surrounding lines on each side of a match
        instead of a follow-up repo_read_file call; context lines are marked with `-` instead of
        `:` after the path and line number, and still count toward max_results."""
        return bounded_service.call(
            "repo_search", repo_id, query, ref, path_glob, max_results, context_lines
        )

    @mcp.tool(title="Read recent commits", annotations=READ_ONLY, structured_output=True)
    def repo_recent_commits(repo_id: str, limit: int = 20) -> dict[str, Any]:
        """Use this when recent history or commit conventions are relevant to the task."""
        return bounded_service.call("repo_recent_commits", repo_id, limit)

    @mcp.tool(
        title="Read GitHub issue",
        description=_MULTILINE_TOOL_DESCRIPTIONS["repo_issue_read"],
        annotations=EXTERNAL_READ,
        structured_output=True,
    )
    def repo_issue_read(repo_id: str, issue_number: int, fresh: bool = False) -> dict[str, Any]:
        """Use this when implementation requirements are defined by a GitHub issue. A recent
        read of the same issue in this session may be served from a short-lived local cache
        (marked `cache_hit: true`); pass `fresh=true` to force a live read, e.g. before acting
        on a check or review that must not be stale."""
        return bounded_service.call("repo_issue_read", repo_id, issue_number, fresh)

    @mcp.tool(title="Query the roadmap ticket graph", annotations=READ_ONLY, structured_output=True)
    def repo_issue_graph(
        repo_id: str,
        root_issue: int | None = None,
        status: str | None = None,
        priority: str | None = None,
        initiative: int | None = None,
    ) -> dict[str, Any]:
        """Use this to list or filter the checked-in roadmap ticket graph without searching GitHub comments; it is offline, bounded, and cannot assign, edit, or close an issue."""
        return bounded_service.call(
            "repo_issue_graph", repo_id, root_issue, status, priority, initiative
        )

    @mcp.tool(
        title="Select the next ready roadmap ticket",
        annotations=EXTERNAL_READ,
        structured_output=True,
    )
    def repo_issue_next(
        repo_id: str,
        root_issue: int | None = None,
        limit: int = 1,
        p0_wip_limit: int = 2,
        p1_wip_limit: int = 3,
        p2_wip_limit: int = 4,
        p3_wip_limit: int = 4,
        initiative_wip_limit: int = 2,
    ) -> dict[str, Any]:
        """Use this to derive selectable tickets from bounded live issue state, complete specs, closed blockers, active parents, WIP limits, and deterministic delivery order without editing GitHub."""
        return bounded_service.call(
            "repo_issue_next",
            repo_id,
            root_issue,
            limit,
            p0_wip_limit,
            p1_wip_limit,
            p2_wip_limit,
            p3_wip_limit,
            initiative_wip_limit,
        )

    @mcp.tool(
        title="Read one roadmap ticket's specification references",
        annotations=EXTERNAL_READ,
        structured_output=True,
    )
    def repo_issue_spec(repo_id: str, issue_number: int, fresh: bool = False) -> dict[str, Any]:
        """Use this before implementing one ticket to get its manifest metadata, the live GitHub issue, drift against the manifest, and comment references without reconstructing prior chat. The live issue portion may be served from a short-lived local cache (marked `cache_hit: true`); pass `fresh=true` to force a live read."""
        return bounded_service.call("repo_issue_spec", repo_id, issue_number, fresh)

    @mcp.tool(
        title="Read GitHub pull request",
        description=_MULTILINE_TOOL_DESCRIPTIONS["repo_pr_read"],
        annotations=EXTERNAL_READ,
        structured_output=True,
    )
    def repo_pr_read(repo_id: str, pr_number: int, fresh: bool = False) -> dict[str, Any]:
        """Use this when reviewing an existing pull request, checks, commits, files, or reviews.
        A recent read of the same pull request in this session may be served from a short-lived
        local cache (marked `cache_hit: true`); pass `fresh=true` to force a live read before
        acting on checks or reviews that must not be stale."""
        return bounded_service.call("repo_pr_read", repo_id, pr_number, fresh)

    @mcp.tool(
        title="Read bounded task-context bundle",
        description=_MULTILINE_TOOL_DESCRIPTIONS["repo_task_context"],
        annotations=EXTERNAL_READ,
        structured_output=True,
    )
    def repo_task_context(
        repo_id: str,
        issue_number: int | None = None,
        workspace_id: str | None = None,
    ) -> dict[str, Any]:
        """Use this when starting or resuming a task to assemble repository context, one
        ticket's specification, workspace status, and recent commits in a single bounded call
        instead of chaining repo_context, repo_issue_spec, workspace_status, and
        repo_recent_commits. Pass issue_number and/or workspace_id to include those sections;
        omitting either yields an explicit null, not an error. A supplied workspace_id must
        belong to repo_id or the call fails closed. The ticket section reuses the same
        short-lived local GitHub read cache as repo_issue_spec. Each section is independently
        bounded and reports its own `truncated` flag, and the whole bundle is capped at 96 KB,
        truncating recent_commits first, then ticket, then workspace, then repository last."""
        return bounded_service.call("repo_task_context", repo_id, issue_number, workspace_id)

    @mcp.tool(
        title="Create isolated coding workspace",
        description=_WORKSPACE_CREATE_DESCRIPTION,
        annotations=LOCAL_CREATE,
        structured_output=True,
    )
    def workspace_create(
        repo_id: str,
        task_slug: str,
        base: str | None = None,
        idempotency_key: str | None = None,
        issue_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """Use this before editing to create an isolated ai/* worktree; use an idempotency key for
        retries. Create one workspace per issue; pass issue_ids only when several dependent
        (stacked) issues are deliberately worked in this same workspace. issue_ids is
        display-only metadata, not validated against any tracker."""
        return bounded_service.call(
            "workspace_create",
            repo_id,
            task_slug,
            base,
            idempotency_key,
            tuple(issue_ids or ()),
        )

    @mcp.tool(
        title="List coding workspaces",
        description=_WORKSPACE_LIST_DESCRIPTION,
        annotations=READ_ONLY,
        structured_output=True,
    )
    def workspace_list() -> dict[str, Any]:
        """Use this when resuming work or finding active RepoForge workspaces; each entry reports age,
        dirty state, and linked issue_ids to help decide what to reuse or remove."""
        return bounded_service.call(
            "workspace_list",
        )

    @mcp.tool(title="Inspect workspace status", annotations=READ_ONLY, structured_output=True)
    def workspace_status(workspace_id: str) -> dict[str, Any]:
        """Use this before writes to refresh HEAD, fingerprint, change budget, and verification state."""
        return bounded_service.call("workspace_status", workspace_id)

    @mcp.tool(
        title="Inspect workspace base freshness",
        annotations=EXTERNAL_READ,
        structured_output=True,
    )
    def workspace_base_status(workspace_id: str) -> dict[str, Any]:
        """Use this to compare the workspace base with configured local and latest remote base state."""
        return bounded_service.call("workspace_base_status", workspace_id)

    @mcp.tool(title="List workspace files", annotations=READ_ONLY, structured_output=True)
    def workspace_tree(workspace_id: str, max_entries: int = 2000) -> dict[str, Any]:
        """Use this when exploring tracked and untracked files allowed by repository policy."""
        return bounded_service.call("workspace_tree", workspace_id, max_entries)

    @mcp.tool(title="Read workspace file", annotations=READ_ONLY, structured_output=True)
    def workspace_read_file(
        workspace_id: str, relative_path: str, start_line: int = 1, end_line: int = 500
    ) -> dict[str, Any]:
        """Use this when reading one UTF-8 file and obtaining its optimistic-lock SHA-256."""
        return bounded_service.call(
            "workspace_read_file", workspace_id, relative_path, start_line, end_line
        )

    @mcp.tool(
        title="Read multiple workspace files",
        annotations=READ_ONLY,
        structured_output=True,
    )
    def workspace_read_files(
        workspace_id: str,
        relative_paths: list[str],
        start_line: int = 1,
        end_line: int = 500,
    ) -> dict[str, Any]:
        """Use this when the same bounded line range is needed from several related files."""
        return bounded_service.call(
            "workspace_read_files", workspace_id, relative_paths, start_line, end_line
        )

    @mcp.tool(
        title="Search workspace code",
        description=_MULTILINE_TOOL_DESCRIPTIONS["workspace_search"],
        annotations=READ_ONLY,
        structured_output=True,
    )
    def workspace_search(
        workspace_id: str,
        query: str,
        path_glob: str | None = None,
        max_results: int = 200,
        context_lines: int = 0,
    ) -> dict[str, Any]:
        """Use this when locating literal text in allowed workspace files; it is not a shell tool.
        Pass context_lines (0-5) to also return that many surrounding lines on each side of a
        match instead of a follow-up workspace_read_file call; context lines are marked with `-`
        instead of `:` after the path and line number, and still count toward max_results."""
        return bounded_service.call(
            "workspace_search", workspace_id, query, path_glob, max_results, context_lines
        )

    @mcp.tool(
        title="Write complete file",
        annotations=LOCAL_DESTRUCTIVE,
        structured_output=True,
    )
    def workspace_write_file(
        workspace_id: str, relative_path: str, content: str, expected_sha256: str
    ) -> dict[str, Any]:
        """Use this to create or fully replace one UTF-8 file with optimistic locking; the response carries a fresh workspace_fingerprint and head_sha for the next locked call, so workspace_status is not required in between."""
        return bounded_service.call(
            "workspace_write_file", workspace_id, relative_path, content, expected_sha256
        )

    @mcp.tool(
        title="Edit files",
        annotations=LOCAL_DESTRUCTIVE,
        structured_output=True,
    )
    def workspace_edit(workspace_id: str, files: list[FileEdit]) -> dict[str, Any]:
        """Use this for precise exact-text replacements across one or more files after validating each file's SHA and occurrence counts; pass one or more file entries, each with its own expected_sha256 and an ordered edits list (up to 20 edits per file, up to 20 files per call). All files are validated before anything is written, so the whole call is atomic -- if any file's SHA or occurrence count doesn't match, nothing is written. The response carries a fresh workspace_fingerprint and head_sha for the next locked call."""
        return bounded_service.call("workspace_edit", workspace_id, files)

    @mcp.tool(
        title="Apply validated patch",
        annotations=LOCAL_DESTRUCTIVE,
        structured_output=True,
    )
    def workspace_apply_patch(
        workspace_id: str,
        patch: str,
        expected_head_sha: str,
        expected_workspace_fingerprint: str,
    ) -> dict[str, Any]:
        """Use this for a git-style unified diff or OpenAI apply_patch envelope against an unchanged workspace; use workspace_edit for exact edits or workspace_write_file for full reviewed content. The response carries a fresh workspace_fingerprint and head_sha for the next locked call."""
        return bounded_service.call(
            "workspace_apply_patch",
            workspace_id,
            patch,
            expected_head_sha,
            expected_workspace_fingerprint,
        )

    @mcp.tool(
        title="Restore selected workspace paths",
        annotations=LOCAL_DESTRUCTIVE,
        structured_output=True,
    )
    def workspace_restore_paths(
        workspace_id: str,
        relative_paths: list[str],
        expected_workspace_fingerprint: str,
    ) -> dict[str, Any]:
        """Use this to undo selected uncommitted tracked changes or remove selected untracked files; the response carries a fresh workspace_fingerprint and head_sha for the next locked call."""
        return bounded_service.call(
            "workspace_restore_paths", workspace_id, relative_paths, expected_workspace_fingerprint
        )

    @mcp.tool(
        title="Preview workspace base refresh",
        annotations=EXTERNAL_READ,
        structured_output=True,
    )
    def workspace_refresh_preview(
        workspace_id: str,
        expected_head_sha: str,
        expected_fingerprint: str,
    ) -> dict[str, Any]:
        """Use this to review one immutable merge preview against the latest configured remote base."""
        return bounded_service.call(
            "workspace_refresh_preview",
            workspace_id,
            expected_head_sha,
            expected_fingerprint,
        )

    @mcp.tool(
        title="Refresh workspace from reviewed base",
        annotations=EXTERNAL_MUTATE,
        structured_output=True,
    )
    def workspace_refresh(
        workspace_id: str,
        preview_id: str,
        expected_head_sha: str,
        expected_fingerprint: str,
    ) -> dict[str, Any]:
        """Use this to merge the exact reviewed base target without rebase, force push, or remote write; the response carries a fresh workspace_fingerprint for the next locked call."""
        return bounded_service.call(
            "workspace_refresh",
            workspace_id,
            preview_id,
            expected_head_sha,
            expected_fingerprint,
        )

    @mcp.tool(title="Inspect workspace diff", annotations=READ_ONLY, structured_output=True)
    def workspace_diff(workspace_id: str, staged: bool = False) -> dict[str, Any]:
        """Use this after edits and before verification, commit, or publishing to review exact changes."""
        return bounded_service.call("workspace_diff", workspace_id, staged)

    @mcp.tool(
        title="Run configured command profile",
        annotations=LOCAL_MUTATE,
        structured_output=True,
    )
    def workspace_run_profile(
        workspace_id: str,
        profile_name: str | None = None,
        background: bool = False,
    ) -> dict[str, Any]:
        """Use this for an allowlisted setup, fix, build, or verification profile. Omit profile_name to run the repository-default verification profile. During the edit-test loop, prefer the quick profile or workspace_run_diagnostic; they are faster and cheaper to run repeatedly. Run the full or repository-default profile only once, right before workspace_commit. The response carries a fresh fingerprint and head_sha for the next locked call. Set background=true for a profile expected to run long: the call validates inputs, holds the workspace lock for the whole run, and returns an operation_id immediately -- poll it with operation_status (and cancel with operation_cancel if needed) instead of blocking this turn. The workspace stays locked to other mutations until the background run finishes."""
        return bounded_service.call("workspace_run_profile", workspace_id, profile_name, background)

    @mcp.tool(
        title="Run reviewed workspace diagnostic",
        annotations=LOCAL_MUTATE,
        structured_output=True,
    )
    def workspace_run_diagnostic(
        workspace_id: str,
        diagnostic_id: str,
        selector: str | list[str] | None = None,
        expected_fingerprint: str | None = None,
        intent: str | None = None,
        expectation: str | None = None,
        expected_failure_class: str | None = None,
        selector2: str | list[str] | None = None,
    ) -> dict[str, Any]:
        """Use this to run one typed repository-reviewed diagnostic; the response carries fingerprint_after and head_sha for the next locked call when the fingerprint changed. Pass a single string or a bounded list of strings for a multi-value selector; use selector2 only when the diagnostic declares a second named placeholder. Call repo_task_context or repo_status first to see each enrolled diagnostic's selector schema (kind, character classes, max_values, expansion) before constructing a call."""
        return bounded_service.call(
            "workspace_run_diagnostic",
            workspace_id,
            diagnostic_id,
            selector,
            expected_fingerprint,
            intent,
            expectation,
            expected_failure_class,
            selector2,
        )

    @mcp.tool(
        title="Run audited ad-hoc command",
        annotations=LOCAL_MUTATE,
        structured_output=True,
    )
    def workspace_run_adhoc(
        workspace_id: str,
        argv: list[str],
        working_directory: str | None = None,
        background: bool = False,
    ) -> dict[str, Any]:
        """Use this only in a repository the owner has explicitly configured with execution_mode="relaxed", when no enrolled workspace_run_diagnostic template fits. argv is a bounded list (no shell, no shell metacharacters expanded) whose first element must be one of the repository's configured adhoc_runners. The result is evidence only: it is fully audited but never satisfies require_verification_before_commit -- run an enrolled verification profile on the exact tree immediately before workspace_commit. In a strict-mode repository this call returns a structured EXECUTION_MODE_STRICT error naming the enrolled-diagnostic and configuration alternatives instead of running anything. Set background=true for a long-running command; poll with operation_status."""
        return bounded_service.call(
            "workspace_run_adhoc", workspace_id, argv, working_directory, background
        )

    @mcp.tool(
        title="Read baseline-aware workspace hygiene",
        annotations=READ_ONLY,
        structured_output=True,
    )
    def workspace_hygiene_status(
        workspace_id: str,
        formatter_id: str | None = None,
    ) -> dict[str, Any]:
        """Use this to compare exact-base and current-workspace formatter findings without mutating repository or workspace state."""
        return bounded_service.call(
            "workspace_hygiene_status",
            workspace_id,
            formatter_id,
        )

    @mcp.tool(
        title="Format policy-allowed changed paths",
        annotations=LOCAL_MUTATE,
        structured_output=True,
    )
    def workspace_format_changed(
        workspace_id: str,
        expected_fingerprint: str,
        formatter_id: str | None = None,
    ) -> dict[str, Any]:
        """Use this after editing to run one reviewed formatter over server-derived policy-allowed changed paths only; callers cannot provide argv, executables, environment values, working directories, or path lists."""
        return bounded_service.call(
            "workspace_format_changed",
            workspace_id,
            expected_fingerprint,
            formatter_id,
        )

    @mcp.tool(
        title="Verify workspace (deprecated alias)",
        annotations=LOCAL_MUTATE,
        structured_output=True,
    )
    def workspace_verify(workspace_id: str, profile_name: str | None = None) -> dict[str, Any]:
        """Use this compatibility alias only while migrating to workspace_run_profile."""
        return bounded_service.call("workspace_verify", workspace_id, profile_name)

    @mcp.tool(
        title="Commit verified changes",
        annotations=LOCAL_CREATE,
        structured_output=True,
    )
    def workspace_commit(workspace_id: str, message: str) -> dict[str, Any]:
        """Use this after successful verification to stage and commit the exact verified tree."""
        return bounded_service.call("workspace_commit", workspace_id, message)

    @mcp.tool(title="Push AI branch", annotations=EXTERNAL_WRITE, structured_output=True)
    def workspace_push(workspace_id: str, idempotency_key: str | None = None) -> dict[str, Any]:
        """Use this after commit to push the allowlisted ai/* branch without force."""
        return bounded_service.call("workspace_push", workspace_id, idempotency_key)

    @mcp.tool(
        title="Create draft pull request",
        annotations=EXTERNAL_WRITE,
        structured_output=True,
    )
    def workspace_create_draft_pr(
        workspace_id: str,
        title: str,
        body: str,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Use this after push to create a draft PR with configured labels and reviewers."""
        return bounded_service.call(
            "workspace_create_draft_pr", workspace_id, title, body, idempotency_key
        )

    @mcp.tool(
        title="Update draft pull request",
        annotations=EXTERNAL_WRITE,
        structured_output=True,
    )
    def workspace_update_draft_pr(
        workspace_id: str,
        title: str | None = None,
        body: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Use this to update the existing workspace PR title or body; it does not mark it ready or merge."""
        return bounded_service.call(
            "workspace_update_draft_pr", workspace_id, title, body, idempotency_key
        )

    @mcp.tool(
        title="Read workspace PR status",
        annotations=EXTERNAL_READ,
        structured_output=True,
    )
    def workspace_pr_status(workspace_id: str) -> dict[str, Any]:
        """Use this to read draft state, mergeability, review decision, and rolled-up checks."""
        return bounded_service.call("workspace_pr_status", workspace_id)

    @mcp.tool(
        title="Read workspace PR checks",
        annotations=EXTERNAL_READ,
        structured_output=True,
    )
    def workspace_pr_checks(workspace_id: str, required_only: bool = False) -> dict[str, Any]:
        """Use this to get compact pass, fail, pending, and skipped CI check buckets."""
        return bounded_service.call("workspace_pr_checks", workspace_id, required_only)

    @mcp.tool(
        title="Watch workspace PR checks",
        annotations=EXTERNAL_MUTATE,
        structured_output=True,
    )
    def workspace_pr_watch(
        workspace_id: str,
        until: str = "all_completed",
        timeout_seconds: int = 900,
        include_failure_evidence: bool = True,
    ) -> dict[str, Any]:
        """Use this to start a durable exact-SHA check watch and return its operation reference."""
        return bounded_service.call(
            "workspace_pr_watch",
            workspace_id,
            until,
            timeout_seconds,
            include_failure_evidence,
        )

    @mcp.tool(
        title="Read structured PR check details",
        annotations=READ_ONLY,
        structured_output=True,
    )
    def workspace_pr_check_details(
        workspace_id: str,
        check_selector: str,
    ) -> dict[str, Any]:
        """Use this with an exact selector from workspace_pr_checks to inspect one Check Run."""
        return bounded_service.call(
            "workspace_pr_check_details",
            workspace_id,
            check_selector,
        )

    @mcp.tool(
        title="Read bounded PR failure evidence",
        annotations=READ_ONLY,
        structured_output=True,
    )
    def workspace_pr_failure_evidence(
        workspace_id: str,
        check_selector: str,
        max_excerpt_lines: int = 80,
    ) -> dict[str, Any]:
        """Use this with a failed check selector to get redacted, bounded diagnostic evidence."""
        return bounded_service.call(
            "workspace_pr_failure_evidence",
            workspace_id,
            check_selector,
            max_excerpt_lines,
        )

    @mcp.tool(
        title="Remove local workspace",
        annotations=LOCAL_DESTRUCTIVE,
        structured_output=True,
    )
    def workspace_remove(workspace_id: str, delete_local_branch: bool = False) -> dict[str, Any]:
        """Use this only after work is complete to remove a clean local worktree; remote data is untouched."""
        return bounded_service.call("workspace_remove", workspace_id, delete_local_branch)

    return mcp
