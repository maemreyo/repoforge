"""Forge v2 MCP composition backed by the authoritative 28-tool registry."""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import secrets
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from pathlib import Path
from typing import Any, NoReturn, cast

from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from mcp.types import Tool as McpTool
from pydantic import BaseModel

from ...application.config_admin import ConfigAdminService
from ...application.read_batch import FileReadRequest
from ...application.retrieval import SearchMode as ApplicationSearchMode
from ...application.runtime.hot_reload import AtomicServiceRouter
from ...application.service import CodingService
from ...application.workspace.mutate import (
    ApplyPatchMutation,
    CreateMutation,
    DeleteMutation,
    MoveMutation,
    ReplaceTextMutation,
    RestoreMutation,
    TextReplacement,
    WorkspaceMutation,
    WriteMutation,
)
from ...config import load_config
from ...contracts.common import ToolResponse
from ...contracts.registry import V2_TOOL_NAMES, V2_TOOL_SPECS, ToolContractSpec
from ...domain.errors import ConfigError, WorkspaceError, operation_error_from_exception
from ...domain.latency import LatencyLayer, LatencyObservation, LatencyTrace
from ...domain.operations import automatic_retry_allowed
from ...domain.redaction import redact_text
from .capabilities import capability_policy_from_context
from .payload import render_tool_payload

FORGE_V2_IDENTITY = "forge_v2"
FORGE_V2_CONTRACT_VERSION = 2

SERVER_INSTRUCTIONS = """
Forge v2 connects ChatGPT to allowlisted local Git repositories through isolated worktrees.
Begin with repo_list, then use repo_task_context before creating or resuming a workspace. The public
surface is the fixed 28-tool Forge v2 contract; retired Forge v1 names are not aliases. Prefer bounded
composite reads, workspace_mutate for exact-state edits, workspace_verify for reviewed diagnostics and
profiles, and workspace_pr for draft-PR lifecycle operations. Review workspace_diff after meaningful
changes. Run final verification immediately before workspace_commit. Never merge, force-push, modify
protected branches, request secrets, or bypass policy. Use config_inspect and runtime_logs_read for
bounded operational evidence, and repo_policy for reviewed policy preview/apply flows.
""".strip()

READ_ONLY = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)
EXTERNAL_READ = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
)
EXTERNAL_MUTATE = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=True,
)
LOCAL_CREATE = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=False,
)
LOCAL_MUTATE = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=False,
)
LOCAL_IDEMPOTENT_MUTATE = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)
LOCAL_DESTRUCTIVE = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=True,
    idempotentHint=False,
    openWorldHint=False,
)
EXTERNAL_WRITE = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=True,
)

_TOOL_TITLES: Mapping[str, str] = {
    "repo_task_context": "Assemble task context",
    "repo_read": "Read repository files",
    "repo_search": "Search repository snapshot",
    "repo_tree": "List repository tree",
    "repo_history": "Read repository history",
    "repo_issue": "Read or change repository issue",
    "repo_pr_read": "Read pull request evidence",
    "repo_list": "List configured repositories",
    "repo_policy": "Preview or apply repository policy",
    "workspace_create": "Create isolated workspace",
    "workspace_remove": "Remove local workspace",
    "workspace_list": "List workspaces",
    "workspace_refresh": "Preview or apply base refresh",
    "workspace_status": "Read workspace status",
    "workspace_format_changed": "Format changed paths",
    "workspace_read": "Read workspace files",
    "workspace_search": "Search workspace files",
    "workspace_tree": "List workspace tree",
    "workspace_diff": "Read workspace diff",
    "workspace_mutate": "Apply exact-state workspace mutations",
    "workspace_verify": "Plan or run workspace verification",
    "workspace_commit": "Commit verified workspace",
    "workspace_push": "Push workspace branch",
    "workspace_pr": "Manage draft pull request",
    "workspace_pr_evidence": "Read pull request evidence",
    "operation": "Read or cancel durable operations",
    "config_inspect": "Inspect reviewed configuration",
    "runtime_logs_read": "Read bounded runtime logs",
}

_TOOL_DESCRIPTIONS: Mapping[str, str] = {
    "repo_task_context": "Return bounded repository, ticket, workspace, and recent-commit context for one task.",
    "repo_read": "Read one or more UTF-8 files from one immutable reviewed repository snapshot.",
    "repo_search": "Run bounded literal, regex, or filename search in one immutable repository snapshot.",
    "repo_tree": "List a bounded repository subtree with resumable cursor evidence.",
    "repo_history": "Read a commit, recent history, or a bounded comparison between two refs.",
    "repo_issue": "Read, plan, graph, create, link, comment on, close, or reopen a GitHub issue through one typed tool.",
    "repo_pr_read": "Read bounded overview, files, checks, reviews, comments, or failure evidence for one pull request.",
    "repo_list": (
        "List configured repositories and optionally include reviewed capability detail. Pass "
        "requested_repo as the exact repo_id, display name, or remote name the user explicitly "
        "named; leave it unset if they did not -- never guess from unrelated wording. Read "
        "selection.outcome: single_enrolled/exact_match means proceed with selection.repo_id "
        "without asking; input_required means ask the user to choose from selection.candidates "
        "(never by recency, filesystem order, default base branch, or your own preference); "
        "no_match means no repository is enrolled yet."
    ),
    "repo_policy": "Preview or apply an exact-state-bound repository policy proposal through the reviewed generation pipeline.",
    "workspace_create": "Create one isolated ai/* worktree for a task or deliberate stacked issue chain.",
    "workspace_remove": "Remove a clean local worktree without touching remote data.",
    "workspace_list": "List bounded workspace lifecycle and cleanup evidence.",
    "workspace_refresh": "Preview or apply a merge-based refresh against the configured remote base.",
    "workspace_status": "Return selected local, base, and verification status sections with exact fingerprints.",
    "workspace_format_changed": "Run reviewed formatters over server-derived changed paths only.",
    "workspace_read": "Read one or more allowed UTF-8 workspace files under one byte budget.",
    "workspace_search": "Run bounded literal, regex, or filename search in allowed workspace files.",
    "workspace_tree": "List a bounded allowed workspace subtree with exact-state evidence.",
    "workspace_diff": "Return a structured bounded diff for the current workspace tree.",
    "workspace_mutate": "Atomically plan or apply typed exact-state mutations under workspace policy and budgets.",
    "workspace_verify": "Plan, route, or run reviewed diagnostics, profiles, or relaxed-mode adhoc verification.",
    "workspace_commit": "Commit only the exact verified tree with optional exact-head and fingerprint locks.",
    "workspace_push": "Push the allowlisted ai/* branch without force and with optional remote-head locking.",
    "workspace_pr": "Create, update, comment on, watch, or otherwise manage the workspace draft pull request.",
    "workspace_pr_evidence": "Read bounded overview, delta, check, review, comment, or failure evidence for the workspace PR.",
    "operation": "Get, list, or request cancellation of durable background operations.",
    "config_inspect": "Inspect accepted and active configuration, effective policy, pending changes, and runtime identity.",
    "runtime_logs_read": "Read bounded redacted audit or managed-runtime log entries with filters and cursors.",
}

_READ_ONLY_TOOLS = frozenset(
    {
        "repo_task_context",
        "repo_read",
        "repo_search",
        "repo_tree",
        "repo_history",
        "repo_list",
        "workspace_list",
        "workspace_status",
        "workspace_read",
        "workspace_search",
        "workspace_tree",
        "workspace_diff",
        "config_inspect",
        "runtime_logs_read",
    }
)
_EXTERNAL_READ_TOOLS = frozenset({"repo_pr_read", "workspace_pr_evidence"})
_EXTERNAL_MUTATE_TOOLS = frozenset({"repo_issue"})
_EXTERNAL_WRITE_TOOLS = frozenset({"workspace_push", "workspace_pr"})
_LOCAL_CREATE_TOOLS = frozenset({"workspace_create"})
_LOCAL_DESTRUCTIVE_TOOLS = frozenset({"workspace_remove"})
_LOCAL_IDEMPOTENT_TOOLS = frozenset({"workspace_format_changed", "workspace_verify", "operation"})


def _tool_annotations(name: str) -> ToolAnnotations:
    if name in _READ_ONLY_TOOLS:
        return READ_ONLY
    if name in _EXTERNAL_READ_TOOLS:
        return EXTERNAL_READ
    if name in _EXTERNAL_MUTATE_TOOLS:
        return EXTERNAL_MUTATE
    if name in _EXTERNAL_WRITE_TOOLS:
        return EXTERNAL_WRITE
    if name in _LOCAL_CREATE_TOOLS:
        return LOCAL_CREATE
    if name in _LOCAL_DESTRUCTIVE_TOOLS:
        return LOCAL_DESTRUCTIVE
    if name in _LOCAL_IDEMPOTENT_TOOLS:
        return LOCAL_IDEMPOTENT_MUTATE
    return LOCAL_MUTATE


class _StructuredMcpToolError(RuntimeError):
    """Signal a stable structured failure while preserving MCP isError semantics.

    `payload` carries the same typed error envelope as the message text so
    `call_tool` can surface it as real `structuredContent` on the wire
    instead of leaving callers to parse JSON out of the text block."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        super().__init__(json.dumps(payload, sort_keys=True, ensure_ascii=False))


def _bounded(text: str, limit: int = 500) -> str:
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _raise_structured_error(
    operation_name: str,
    exc: Exception,
    *,
    has_idempotency_key: bool = False,
) -> NoReturn:
    envelope = operation_error_from_exception(exc)
    correlation_id = envelope.correlation_id or secrets.token_hex(12)
    message = redact_text(
        envelope.what_happened,
        secrets=(os.environ.get("CONTROL_PLANE_API_KEY", ""),),
    )
    unchanged_state = tuple(
        _bounded(item)
        for item in (
            list(envelope.unchanged_state) or ["No unreported state transition was committed."]
        )[:20]
    )
    # This is the same {status, summary, error: ToolError} shape every one of
    # the 28 tools' own output model inherits from ToolResponse -- a client
    # can validate an error response against the shared base contract, not
    # only recover ad-hoc fields by name (#225 review: the earlier flat
    # envelope did not conform to any advertised output schema).
    payload = {
        "status": "failed",
        "summary": _bounded(message),
        "error": {
            "code": envelope.code.value,
            "message": _bounded(message),
            "why": _bounded(envelope.why),
            "retryable": envelope.retryable,
            "safe_next_action": _bounded(envelope.safe_next_action),
            "details": {"correlation_id": correlation_id},
            "unchanged_state": unchanged_state,
            "automatic_retry_allowed": automatic_retry_allowed(
                operation_name,
                envelope.code,
                has_idempotency_key=has_idempotency_key,
            ),
        },
    }
    raise _StructuredMcpToolError(payload) from exc


class _ServiceErrorBoundary:
    """Pin one service generation and convert application failures to one error envelope."""

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
        self._bound_service: ContextVar[Any | None] = ContextVar(
            f"repoforge_bound_service_{id(self)}",
            default=None,
        )

    @contextmanager
    def _acquire_service(self) -> Iterator[Any]:
        if self._router is None:
            yield self._service
            return
        with self._router.acquire() as container:
            yield container.service

    @contextmanager
    def bind_request_service(self) -> Iterator[Any]:
        with self._acquire_service() as service:
            token = self._bound_service.set(service)
            try:
                yield service
            finally:
                self._bound_service.reset(token)

    @contextmanager
    def _selected_service(self) -> Iterator[Any]:
        bound = self._bound_service.get()
        if bound is not None:
            yield bound
            return
        with self._acquire_service() as service:
            yield service

    def call(self, name: str, **kwargs: Any) -> dict[str, Any]:
        has_idempotency_key = bool(kwargs.get("idempotency_key"))
        try:
            with self._selected_service() as service:
                target = getattr(service, name)
                result = target(**kwargs)
            if not isinstance(result, dict):
                raise TypeError("MCP service operation must return an object")
            return result
        except Exception as exc:
            _raise_structured_error(
                name,
                exc,
                has_idempotency_key=has_idempotency_key,
            )


class _UnavailableConfigAdmin:
    def __getattr__(self, name: str) -> Any:
        raise ConfigError(
            "CONFIG_ADMIN_UNAVAILABLE: configuration administration is not available on this "
            "transport. Run the server through `rf serve` or the managed runtime."
        )


_SERVICE_METHODS: Mapping[str, str] = {
    "repo_task_context": "repo_task_context_v2",
    "repo_read": "repo_read",
    "repo_search": "repo_search_v2",
    "repo_tree": "repo_tree_v2",
    "repo_history": "repo_history_v2",
    "repo_issue": "repo_issue_v2",
    "repo_pr_read": "repo_pr_read_v2",
    "repo_list": "repo_list_v2",
    "workspace_create": "workspace_create_v2",
    "workspace_remove": "workspace_remove_v2",
    "workspace_list": "workspace_list_v2",
    "workspace_refresh": "workspace_refresh_v2",
    "workspace_status": "workspace_status_v2",
    "workspace_format_changed": "workspace_format_changed_v2",
    "workspace_read": "workspace_read",
    "workspace_search": "workspace_search_v2",
    "workspace_tree": "workspace_tree_v2",
    "workspace_diff": "workspace_diff_v2",
    "workspace_mutate": "workspace_mutate",
    "workspace_verify": "workspace_verify",
    "workspace_commit": "workspace_commit",
    "workspace_push": "workspace_push",
    "workspace_pr": "workspace_pr",
    "workspace_pr_evidence": "workspace_pr_evidence",
    "operation": "operation",
}
_ADMIN_METHODS: Mapping[str, str] = {
    "repo_policy": "repo_policy",
    "config_inspect": "config_inspect_v2",
    "runtime_logs_read": "runtime_logs_read_v2",
}


def _read_requests(raw: list[dict[str, Any]]) -> list[FileReadRequest]:
    return [
        FileReadRequest(
            path=str(item["path"]),
            start_line=int(item.get("start_line", 1)),
            end_line=int(item.get("end_line", 500)),
        )
        for item in raw
    ]


def _mutation_operations(raw: list[dict[str, Any]]) -> list[WorkspaceMutation]:
    operations: list[WorkspaceMutation] = []
    for item in raw:
        op = item["op"]
        if op == "replace_text":
            operations.append(
                ReplaceTextMutation(
                    path=item["path"],
                    expected_sha256=item["expected_sha256"],
                    edits=tuple(
                        TextReplacement(
                            old_text=edit["old_text"],
                            new_text=edit["new_text"],
                            expected_occurrences=edit.get("expected_occurrences", 1),
                        )
                        for edit in item["edits"]
                    ),
                )
            )
        elif op == "write":
            operations.append(
                WriteMutation(
                    path=item["path"],
                    content=item["content"],
                    expected_sha256=item["expected_sha256"],
                )
            )
        elif op == "create":
            operations.append(
                CreateMutation(
                    path=item["path"],
                    content=item["content"],
                    mode=item.get("mode", 0o644),
                )
            )
        elif op == "delete":
            operations.append(DeleteMutation(item["path"], item["expected_sha256"]))
        elif op == "move":
            operations.append(
                MoveMutation(
                    item["source"],
                    item["destination"],
                    item["expected_source_sha256"],
                )
            )
        elif op == "apply_patch":
            operations.append(ApplyPatchMutation(item["patch"]))
        elif op == "restore":
            operations.append(RestoreMutation(tuple(item["paths"])))
        else:  # pragma: no cover - discriminated Pydantic input prevents this
            raise ValueError(f"Unsupported mutation operation: {op}")
    return operations


def _dispatch_kwargs(tool_name: str, model: BaseModel) -> dict[str, Any]:
    kwargs = model.model_dump(mode="json")
    if tool_name in {"repo_read", "workspace_read"}:
        kwargs["files"] = _read_requests(kwargs["files"])
    if tool_name in {"repo_search", "workspace_search"}:
        kwargs["mode"] = ApplicationSearchMode(kwargs["mode"])
    if tool_name == "repo_policy" and kwargs["action"] == "apply":
        for field in ("mutations", "generated_paths", "issue_writes"):
            if field not in model.model_fields_set:
                kwargs.pop(field, None)
    if tool_name == "workspace_create":
        kwargs["issue_ids"] = tuple(kwargs["issue_ids"])
    if tool_name == "workspace_mutate":
        kwargs["operations"] = _mutation_operations(kwargs["operations"])
    return kwargs


def _public_output(tool_name: str, raw: dict[str, Any]) -> dict[str, Any]:
    """Adapt domain evidence to the strict public envelope without hiding failures."""

    payload = dict(raw)
    if tool_name in {"repo_read", "workspace_read"}:
        errors = payload.pop("errors", [])
        requested = int(payload.pop("requested", len(payload.get("files", []))))
        succeeded = int(payload.pop("succeeded", len(payload.get("files", []))))
        if errors:
            first = errors[0] if isinstance(errors, list) else errors
            raise WorkspaceError(
                f"{tool_name.upper()}_PARTIAL_FAILURE: {first}; "
                f"succeeded {succeeded} of {requested} requested files"
            )
        files: list[dict[str, Any]] = []
        for item in payload.get("files", []):
            public_item = dict(item)
            public_item.pop("size_bytes", None)
            files.append(public_item)
        payload["files"] = files

    payload.setdefault("status", "ok")
    payload.setdefault("error", None)
    if "summary" not in payload:
        count = len(payload.get("files", payload.get("matches", payload.get("entries", []))))
        noun = "item" if count == 1 else "items"
        payload["summary"] = f"{tool_name} completed with {count} {noun}"
    return payload


class ForgeV2FastMCP(FastMCP[None]):
    """Publish and execute only the static Forge v2 registry."""

    def __init__(
        self,
        *,
        service_boundary: _ServiceErrorBoundary,
        admin_boundary: _ServiceErrorBoundary,
    ) -> None:
        super().__init__(
            FORGE_V2_IDENTITY,
            instructions=SERVER_INSTRUCTIONS,
            log_level="WARNING",
        )
        self._service_boundary = service_boundary
        self._admin_boundary = admin_boundary

    async def list_tools(self) -> list[McpTool]:
        return [
            McpTool(
                name=name,
                title=_TOOL_TITLES[name],
                description=_TOOL_DESCRIPTIONS[name],
                inputSchema=V2_TOOL_SPECS[name].input_model.model_json_schema(mode="validation"),
                outputSchema=V2_TOOL_SPECS[name].output_model.model_json_schema(mode="validation"),
                annotations=_tool_annotations(name),
            )
            for name in V2_TOOL_NAMES
        ]

    def _dispatch(self, tool_name: str, kwargs: dict[str, Any]) -> dict[str, Any]:
        if tool_name in _ADMIN_METHODS:
            return self._admin_boundary.call(_ADMIN_METHODS[tool_name], **kwargs)
        method = _SERVICE_METHODS[tool_name]
        if tool_name == "workspace_mutate":
            expected_head_sha = kwargs.pop("expected_head_sha")
            status = self._service_boundary.call(
                "workspace_status_v2",
                workspace_id=kwargs["workspace_id"],
                sections=("local",),
                byte_budget=60_000,
            )
            if status.get("head_sha") != expected_head_sha:
                raise WorkspaceError(
                    "STALE_WORKSPACE_HEAD: expected_head_sha does not match current HEAD"
                )
        result = self._service_boundary.call(method, **kwargs)
        if tool_name == "repo_list":
            selection = result.get("selection")
            if isinstance(selection, dict) and selection.get("outcome") == "input_required":
                policy = capability_policy_from_context(self.get_context())
                candidates = cast("list[dict[str, Any]]", selection.get("candidates", []))
                result["selection_prompt"] = policy.input_required(
                    decision_id="repo_selection",
                    prompt=cast(str, selection.get("guidance", "")),
                    allowed_options=tuple(candidate["repo_id"] for candidate in candidates),
                )
        return result

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        if name not in V2_TOOL_SPECS:
            raise ValueError(f"Unknown Forge v2 tool: {name}")
        spec = V2_TOOL_SPECS[name]
        try:
            return await self._call_tool(name, arguments, spec)
        except _StructuredMcpToolError as exc:
            # The typed error envelope belongs in structuredContent, not only
            # serialized into the text block -- otherwise a client can only
            # recover it by parsing JSON out of free text (#225 review).
            # Validate against the same ToolResponse/ToolError base contract
            # every one of the 28 tools' own output model inherits, so a
            # future shape drift fails loudly here instead of silently
            # shipping structuredContent a client cannot parse against the
            # advertised schema.
            ToolResponse.model_validate(exc.payload)
            return CallToolResult(
                content=[TextContent(type="text", text=str(exc))],
                structuredContent=exc.payload,
                isError=True,
            )

    async def _call_tool(
        self, name: str, arguments: dict[str, Any], spec: ToolContractSpec
    ) -> CallToolResult:
        try:
            validated_input = spec.validate_input(arguments)
        except Exception as exc:
            _raise_structured_error(name, exc)

        with self._service_boundary.bind_request_service() as service:
            started = time.perf_counter()
            try:
                raw = self._dispatch(name, _dispatch_kwargs(name, validated_input))
                validated_output = spec.validate_output(_public_output(name, raw))
                structured = validated_output.model_dump(mode="json", by_alias=True)
            except _StructuredMcpToolError:
                raise
            except Exception as exc:
                _raise_structured_error(
                    name,
                    exc,
                    has_idempotency_key=bool(arguments.get("idempotency_key")),
                )
            engine_ms = (time.perf_counter() - started) * 1_000.0

            server_config = getattr(getattr(service, "config", None), "server", None)
            legacy_duplication = bool(
                getattr(server_config, "legacy_text_result_duplication", False)
            )
            rendered = render_tool_payload(
                name,
                structured,
                legacy_text_result_duplication=legacy_duplication,
            )

            client_name = "unknown"
            client_version = "unknown"
            try:
                params = self.get_context().session.client_params
                client_info = getattr(params, "clientInfo", None) or getattr(
                    params, "client_info", None
                )
                if client_info is not None:
                    client_name = str(getattr(client_info, "name", client_name))
                    client_version = str(getattr(client_info, "version", client_version))
            except (LookupError, AttributeError):
                pass

            trace = LatencyTrace(
                trace_id=f"trace-{secrets.token_hex(16)}",
                tool_name=name,
                tool_class=rendered.tool_class,
                client_name=client_name,
                client_version=client_version,
                engine=LatencyObservation.observed(LatencyLayer.ENGINE, engine_ms),
                connector=LatencyObservation.unobserved(LatencyLayer.CONNECTOR),
                client_round_trip=LatencyObservation.unobserved(LatencyLayer.CLIENT_ROUND_TRIP),
                payload=rendered.metrics,
            )
            with suppress(Exception):
                service.metrics.record_latency(trace)

            return CallToolResult(
                content=rendered.content,
                structuredContent=rendered.structured,
                isError=False,
                _meta={"repoforge_trace": trace.as_dict()},
            )


def _surface_payload() -> dict[str, Any]:
    return {
        "schema_version": 3,
        "identity": FORGE_V2_IDENTITY,
        "contract_version": FORGE_V2_CONTRACT_VERSION,
        "tools": [
            {
                "name": name,
                "title": _TOOL_TITLES[name],
                "description": inspect.cleandoc(_TOOL_DESCRIPTIONS[name]),
                "annotations": _tool_annotations(name).model_dump(
                    mode="json", by_alias=True, exclude_none=True
                ),
                "input_schema": V2_TOOL_SPECS[name].input_model.model_json_schema(
                    mode="validation"
                ),
                "output_schema": V2_TOOL_SPECS[name].output_model.model_json_schema(
                    mode="validation"
                ),
            }
            for name in V2_TOOL_NAMES
        ],
    }


def tool_surface_hash(contract_version: int | None = None) -> str:
    if contract_version not in {None, FORGE_V2_CONTRACT_VERSION}:
        raise ValueError("Forge v2 server only supports contract v2")
    return hashlib.sha256(
        json.dumps(
            _surface_payload(),
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def create_server(
    config_path: str | Path | None = None,
    *,
    service: CodingService | None = None,
    router: AtomicServiceRouter | None = None,
    contract_version: int | None = None,
    admin: ConfigAdminService | None = None,
) -> FastMCP:
    if contract_version not in {None, FORGE_V2_CONTRACT_VERSION}:
        raise ValueError("Forge v2 server only supports contract v2")
    if service is not None and router is not None:
        raise ValueError("create_server accepts either service or router, not both")
    raw_service = service or (
        None if router is not None else CodingService(load_config(config_path))
    )
    service_boundary = _ServiceErrorBoundary(raw_service, router=router)
    admin_boundary = _ServiceErrorBoundary(
        admin if admin is not None else _UnavailableConfigAdmin()
    )
    return ForgeV2FastMCP(
        service_boundary=service_boundary,
        admin_boundary=admin_boundary,
    )


__all__ = [
    "FORGE_V2_CONTRACT_VERSION",
    "FORGE_V2_IDENTITY",
    "SERVER_INSTRUCTIONS",
    "ForgeV2FastMCP",
    "_ServiceErrorBoundary",
    "create_server",
    "tool_surface_hash",
]
