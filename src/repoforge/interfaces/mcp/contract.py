"""Deterministic public release contract for drift detection and Plugin compatibility."""

from __future__ import annotations

import hashlib
import inspect
from typing import Any, cast

from ... import __version__
from ...application.configuration.document import RESOLVED_CONFIG_FORMAT_VERSION
from ...application.configuration.source import SOURCE_CONFIG_VERSION
from ...application.diagnostics.bundle import DIAGNOSTICS_SCHEMA_VERSION
from ...application.service import CodingService
from ...domain.runtime import RUNTIME_CONTROL_PROTOCOL_VERSION
from ...domain.tool_contract import default_tool_contract_registry
from .server import SERVER_INSTRUCTIONS, create_server, tool_surface_hash

RELEASE_CONTRACT_VERSION = 2


async def build_release_contract() -> dict[str, Any]:
    """Return the byte-stable public contract that must be reviewed for every release."""
    registry = default_tool_contract_registry()
    server = create_server(
        service=cast(CodingService, object()),
        contract_version=registry.current_version,
    )
    tools = await server.list_tools()

    def _normalise(tool: Any) -> dict[str, Any]:
        raw: dict[str, Any] = tool.model_dump(mode="json", by_alias=True, exclude_none=True)
        if isinstance(raw.get("description"), str):
            raw["description"] = inspect.cleandoc(raw["description"])
        return raw

    normalized_tools = sorted(
        (_normalise(tool) for tool in tools),
        key=lambda item: str(item["name"]),
    )
    legacy_server = create_server(
        service=cast(CodingService, object()),
        contract_version=registry.supported_versions[0],
    )
    legacy_tools = await legacy_server.list_tools()
    alias_names = {alias.alias for alias in registry.aliases}
    legacy_alias_tools = sorted(
        (_normalise(tool) for tool in legacy_tools if tool.name in alias_names),
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
            "tool_surface_hash": tool_surface_hash(registry.current_version),
            "tool_contract": {
                "current_version": registry.current_version,
                "supported_versions": list(registry.supported_versions),
                "aliases": [alias.as_dict() for alias in registry.aliases],
                "legacy_alias_tools": legacy_alias_tools,
            },
            "tools": normalized_tools,
        },
    }
