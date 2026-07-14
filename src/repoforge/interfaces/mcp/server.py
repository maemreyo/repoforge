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
from mcp.types import ToolAnnotations

from ...application.runtime.hot_reload import AtomicServiceRouter
from ...application.service import CodingService
from ...config import load_config
from ...domain.errors import operation_error_from_exception
from ...domain.operations import automatic_retry_allowed
from ...domain.redaction import redact_text


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
                "automatic_retry_allowed": automatic_retry_allowed(
                    name,
                    envelope.code,
                    has_idempotency_key=has_idempotency_key,
                ),
            }
            raise _StructuredMcpToolError(
                json.dumps(payload, sort_keys=True, ensure_ascii=False)
            ) from exc


SERVER_INSTRUCTIONS = "RepoForge connects ChatGPT to allowlisted local Git repositories through isolated worktrees.\nAlways begin with repo_list and repo_context, then create one workspace per task. Inspect before editing.\nPrefer exact text replacement or a small validated patch. Review workspace_diff after every meaningful\nchange. Run workspace_verify before commit; never claim verification succeeded unless the tool returned\nsuccess. Commit, push, and create only draft pull requests. Never merge, force-push, modify protected\nbranches, request secrets, or bypass path/change-budget policies. Use workspace_restore_paths to safely\nundo selected uncommitted mistakes after refreshing status.".strip()
READ_ONLY = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
)
EXTERNAL_READ = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True
)
LOCAL_CREATE = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False
)
LOCAL_MUTATE = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False
)
LOCAL_DESTRUCTIVE = ToolAnnotations(
    readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False
)
EXTERNAL_WRITE = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True
)


def tool_surface_hash() -> str:
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
        keywords = {k.arg: ast.unparse(k.value) for k in decorator.keywords if k.arg is not None}
        tools.append(
            {
                "name": node.name,
                "arguments": ast.unparse(node.args),
                "returns": ast.unparse(node.returns) if node.returns else "",
                "title": keywords.get("title", ""),
                "annotations": keywords.get("annotations", ""),
                "structured_output": keywords.get("structured_output", ""),
            }
        )
    return hashlib.sha256(
        json.dumps(sorted(tools, key=lambda x: x["name"]), sort_keys=True).encode()
    ).hexdigest()


def create_server(
    config_path: str | Path | None = None,
    *,
    service: CodingService | None = None,
    router: AtomicServiceRouter | None = None,
) -> FastMCP:
    if service is not None and router is not None:
        raise ValueError("create_server accepts either service or router, not both")
    raw_service = service or (
        None if router is not None else CodingService(load_config(config_path))
    )
    bounded_service = _ServiceErrorBoundary(raw_service, router=router)
    mcp = FastMCP("RepoForge", instructions=SERVER_INSTRUCTIONS, log_level="WARNING")

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
        title="Search committed repository code", annotations=READ_ONLY, structured_output=True
    )
    def repo_search(
        repo_id: str,
        query: str,
        ref: str | None = None,
        path_glob: str | None = None,
        max_results: int = 200,
    ) -> dict[str, Any]:
        """Use this to locate literal text in an immutable reviewed repository snapshot."""
        return bounded_service.call("repo_search", repo_id, query, ref, path_glob, max_results)

    @mcp.tool(title="Read recent commits", annotations=READ_ONLY, structured_output=True)
    def repo_recent_commits(repo_id: str, limit: int = 20) -> dict[str, Any]:
        """Use this when recent history or commit conventions are relevant to the task."""
        return bounded_service.call("repo_recent_commits", repo_id, limit)

    @mcp.tool(title="Read GitHub issue", annotations=EXTERNAL_READ, structured_output=True)
    def repo_issue_read(repo_id: str, issue_number: int) -> dict[str, Any]:
        """Use this when implementation requirements are defined by a GitHub issue."""
        return bounded_service.call("repo_issue_read", repo_id, issue_number)

    @mcp.tool(
        title="Read GitHub pull request",
        annotations=EXTERNAL_READ,
        structured_output=True,
    )
    def repo_pr_read(repo_id: str, pr_number: int) -> dict[str, Any]:
        """Use this when reviewing an existing pull request, checks, commits, files, or reviews."""
        return bounded_service.call("repo_pr_read", repo_id, pr_number)

    @mcp.tool(
        title="Create isolated coding workspace",
        annotations=LOCAL_CREATE,
        structured_output=True,
    )
    def workspace_create(
        repo_id: str,
        task_slug: str,
        base: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Use this before editing to create an isolated ai/* worktree; use an idempotency key for retries."""
        return bounded_service.call("workspace_create", repo_id, task_slug, base, idempotency_key)

    @mcp.tool(title="List coding workspaces", annotations=READ_ONLY, structured_output=True)
    def workspace_list() -> dict[str, Any]:
        """Use this when resuming work or finding active RepoForge workspaces."""
        return bounded_service.call(
            "workspace_list",
        )

    @mcp.tool(title="Inspect workspace status", annotations=READ_ONLY, structured_output=True)
    def workspace_status(workspace_id: str) -> dict[str, Any]:
        """Use this before writes to refresh HEAD, fingerprint, change budget, and verification state."""
        return bounded_service.call("workspace_status", workspace_id)

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

    @mcp.tool(title="Search workspace code", annotations=READ_ONLY, structured_output=True)
    def workspace_search(
        workspace_id: str,
        query: str,
        path_glob: str | None = None,
        max_results: int = 200,
    ) -> dict[str, Any]:
        """Use this when locating literal text in allowed workspace files; it is not a shell tool."""
        return bounded_service.call("workspace_search", workspace_id, query, path_glob, max_results)

    @mcp.tool(
        title="Write complete file",
        annotations=LOCAL_DESTRUCTIVE,
        structured_output=True,
    )
    def workspace_write_file(
        workspace_id: str, relative_path: str, content: str, expected_sha256: str
    ) -> dict[str, Any]:
        """Use this to create or fully replace one UTF-8 file with optimistic locking."""
        return bounded_service.call(
            "workspace_write_file", workspace_id, relative_path, content, expected_sha256
        )

    @mcp.tool(
        title="Replace exact text",
        annotations=LOCAL_DESTRUCTIVE,
        structured_output=True,
    )
    def workspace_replace_text(
        workspace_id: str,
        relative_path: str,
        old_text: str,
        new_text: str,
        expected_sha256: str,
        expected_occurrences: int = 1,
    ) -> dict[str, Any]:
        """Use this for a precise replacement after validating the file SHA and occurrence count."""
        return bounded_service.call(
            "workspace_replace_text",
            workspace_id,
            relative_path,
            old_text,
            new_text,
            expected_sha256,
            expected_occurrences,
        )

    @mcp.tool(
        title="Apply unified patch",
        annotations=LOCAL_DESTRUCTIVE,
        structured_output=True,
    )
    def workspace_apply_patch(
        workspace_id: str,
        patch: str,
        expected_head_sha: str,
        expected_workspace_fingerprint: str,
    ) -> dict[str, Any]:
        """Use this for a validated multi-file unified patch against an unchanged workspace snapshot."""
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
        """Use this to undo selected uncommitted tracked changes or remove selected untracked files."""
        return bounded_service.call(
            "workspace_restore_paths", workspace_id, relative_paths, expected_workspace_fingerprint
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
    def workspace_run_profile(workspace_id: str, profile_name: str) -> dict[str, Any]:
        """Use this for an explicitly named allowlisted setup, fix, build, or verification profile."""
        return bounded_service.call("workspace_run_profile", workspace_id, profile_name)

    @mcp.tool(title="Verify workspace", annotations=LOCAL_MUTATE, structured_output=True)
    def workspace_verify(workspace_id: str, profile_name: str | None = None) -> dict[str, Any]:
        """Use this before commit to run the repository-default or explicitly named verification gate."""
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
        title="Remove local workspace",
        annotations=LOCAL_DESTRUCTIVE,
        structured_output=True,
    )
    def workspace_remove(workspace_id: str, delete_local_branch: bool = False) -> dict[str, Any]:
        """Use this only after work is complete to remove a clean local worktree; remote data is untouched."""
        return bounded_service.call("workspace_remove", workspace_id, delete_local_branch)

    return mcp
