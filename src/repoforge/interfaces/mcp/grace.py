"""Bounded retired-identity shim for Forge v1 connector migration."""

from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import suppress
from typing import Any, Literal

from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from mcp.types import Tool as McpTool
from pydantic import BaseModel, ConfigDict, Field

from .server import FORGE_V2_IDENTITY, tool_surface_hash

FORGE_V1_IDENTITY = "forge_v1"
_GRACE_TOOL = "migration_required"
_LOG = logging.getLogger(__name__)


class GraceInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    reported_surface_hash: str | None = Field(default=None, min_length=1, max_length=256)


class GraceOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["failed"] = "failed"
    error_code: Literal["CONNECTOR_RETIRED"] = "CONNECTOR_RETIRED"
    message: str
    retired_identity: Literal["forge_v1"] = "forge_v1"
    new_identity: Literal["forge_v2"] = "forge_v2"
    expected_surface_hash: str
    reported_surface_hash: str | None = None
    surface_mismatch: bool
    shutdown_required: Literal[True] = True
    safe_next_action: str


class ForgeV1GraceFastMCP(FastMCP[None]):
    """Expose exactly one typed migration error and request graceful shutdown."""

    def __init__(
        self,
        *,
        on_stale_caller: Callable[[dict[str, object]], None] | None = None,
        request_shutdown: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(
            FORGE_V1_IDENTITY,
            instructions=(
                "This connector identity is retired. Call migration_required once, migrate the "
                "client configuration to forge_v2, then reconnect and confirm the v2 surface hash."
            ),
            log_level="WARNING",
        )
        self._on_stale_caller = on_stale_caller
        self._request_shutdown = request_shutdown

    async def list_tools(self) -> list[McpTool]:
        return [
            McpTool(
                name=_GRACE_TOOL,
                title="Migrate retired Forge v1 connector",
                description=(
                    "Return the typed retirement error, target forge_v2 identity, expected v2 "
                    "surface hash, and shutdown instruction."
                ),
                inputSchema=GraceInput.model_json_schema(mode="validation"),
                outputSchema=GraceOutput.model_json_schema(mode="validation"),
                annotations=ToolAnnotations(
                    readOnlyHint=True,
                    destructiveHint=False,
                    idempotentHint=True,
                    openWorldHint=False,
                ),
            )
        ]

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        if name != _GRACE_TOOL:
            raise ValueError(f"Retired {FORGE_V1_IDENTITY} exposes only {_GRACE_TOOL}")
        request = GraceInput.model_validate(arguments)
        expected = tool_surface_hash()
        mismatch = request.reported_surface_hash != expected
        payload = GraceOutput(
            message=(
                "The forge_v1 connector identity is retired and cannot execute repository or "
                "workspace operations."
            ),
            expected_surface_hash=expected,
            reported_surface_hash=request.reported_surface_hash,
            surface_mismatch=mismatch,
            safe_next_action=(
                "Replace the connector identity with forge_v2, reconnect the client, and verify "
                f"that discovery reports surface hash {expected}."
            ),
        ).model_dump(mode="json")
        event: dict[str, object] = {
            "event": "retired_connector_call",
            "retired_identity": FORGE_V1_IDENTITY,
            "new_identity": FORGE_V2_IDENTITY,
            "expected_surface_hash": expected,
            "reported_surface_hash": request.reported_surface_hash,
            "surface_mismatch": mismatch,
        }
        _LOG.warning("retired Forge v1 caller: %s", event)
        if self._on_stale_caller is not None:
            with suppress(Exception):
                self._on_stale_caller(event)
        if self._request_shutdown is not None:
            with suppress(Exception):
                self._request_shutdown()
        return CallToolResult(
            content=[
                TextContent(
                    type="text",
                    text=(
                        f"{FORGE_V1_IDENTITY} is retired; migrate to {FORGE_V2_IDENTITY} and "
                        "reconnect."
                    ),
                )
            ],
            structuredContent=payload,
            isError=False,
        )


def create_grace_server(
    *,
    on_stale_caller: Callable[[dict[str, object]], None] | None = None,
    request_shutdown: Callable[[], None] | None = None,
) -> FastMCP:
    return ForgeV1GraceFastMCP(
        on_stale_caller=on_stale_caller,
        request_shutdown=request_shutdown,
    )


__all__ = [
    "FORGE_V1_IDENTITY",
    "ForgeV1GraceFastMCP",
    "GraceInput",
    "GraceOutput",
    "create_grace_server",
]
