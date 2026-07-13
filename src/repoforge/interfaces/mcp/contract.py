"""Deterministic public release contract for drift detection and Plugin compatibility."""

from __future__ import annotations

import hashlib
from typing import Any, cast

from ... import __version__
from ...application.configuration.document import RESOLVED_CONFIG_FORMAT_VERSION
from ...application.configuration.source import SOURCE_CONFIG_VERSION
from ...application.diagnostics.bundle import DIAGNOSTICS_SCHEMA_VERSION
from ...application.service import CodingService
from ...domain.runtime import RUNTIME_CONTROL_PROTOCOL_VERSION
from .server import SERVER_INSTRUCTIONS, create_server, tool_surface_hash

RELEASE_CONTRACT_VERSION = 1


async def build_release_contract() -> dict[str, Any]:
    """Return the byte-stable public contract that must be reviewed for every release."""
    server = create_server(service=cast(CodingService, object()))
    tools = await server.list_tools()
    normalized_tools = sorted(
        (
            tool.model_dump(
                mode="json",
                by_alias=True,
                exclude_none=True,
            )
            for tool in tools
        ),
        key=lambda item: str(item["name"]),
    )
    return {
        "contract_version": RELEASE_CONTRACT_VERSION,
        "package_version": __version__,
        "configuration": {
            "source_version": SOURCE_CONFIG_VERSION,
            "resolved_format_version": RESOLVED_CONFIG_FORMAT_VERSION,
        },
        "runtime": {
            "control_protocol_version": RUNTIME_CONTROL_PROTOCOL_VERSION,
            "diagnostics_schema_version": DIAGNOSTICS_SCHEMA_VERSION,
        },
        "mcp": {
            "server_instructions_sha256": hashlib.sha256(
                SERVER_INSTRUCTIONS.encode("utf-8")
            ).hexdigest(),
            "tool_surface_hash": tool_surface_hash(),
            "tools": normalized_tools,
        },
    }
