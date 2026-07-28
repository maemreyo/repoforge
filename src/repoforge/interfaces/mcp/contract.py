"""Deterministic Forge v2 release contract for drift detection."""

from __future__ import annotations

import hashlib
import inspect
import json
from typing import Any, cast

from ... import __version__
from ...application.configuration.document import RESOLVED_CONFIG_FORMAT_VERSION
from ...application.configuration.source import SOURCE_CONFIG_VERSION
from ...application.diagnostics.bundle import DIAGNOSTICS_SCHEMA_VERSION
from ...application.service import CodingService
from ...contracts.registry import (
    V2_TOOL_NAMES,
    contract_schema_digests,
    render_v2_schema_bundle,
)
from ...domain.runtime import RUNTIME_CONTROL_PROTOCOL_VERSION
from .grace import FORGE_V1_IDENTITY, create_grace_server
from .server import (
    FORGE_V2_CONTRACT_VERSION,
    FORGE_V2_IDENTITY,
    SERVER_INSTRUCTIONS,
    create_server,
    tool_surface_hash,
)

RELEASE_CONTRACT_VERSION = 2


def _normalise_tool(tool: Any) -> dict[str, Any]:
    raw: dict[str, Any] = tool.model_dump(mode="json", by_alias=True, exclude_none=True)
    if isinstance(raw.get("description"), str):
        raw["description"] = inspect.cleandoc(raw["description"])
    raw.pop("_meta", None)
    return raw


def _hash_json(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _tool_hashes(tools: list[dict[str, Any]]) -> dict[str, str]:
    return {str(tool["name"]): _hash_json(tool) for tool in tools}


async def build_release_contract() -> dict[str, Any]:
    """Return the byte-stable public contract reviewed for every release."""

    server = create_server(service=cast(CodingService, object()))
    tools = [_normalise_tool(tool) for tool in await server.list_tools()]
    grace = create_grace_server()
    grace_tools = [_normalise_tool(tool) for tool in await grace.list_tools()]
    digests = contract_schema_digests()
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
            "identity": FORGE_V2_IDENTITY,
            "retired_identity": FORGE_V1_IDENTITY,
            "server_instructions_sha256": hashlib.sha256(
                SERVER_INSTRUCTIONS.encode("utf-8")
            ).hexdigest(),
            "tool_surface_hash": tool_surface_hash(),
            "input_contract_digest": digests.input_digest,
            "output_contract_digest": digests.output_digest,
            "tool_count": len(tools),
            "tool_names": list(V2_TOOL_NAMES),
            "tool_hashes": _tool_hashes(tools),
            "tool_schema_bundle_sha256": _hash_json(render_v2_schema_bundle()),
            "tool_contract": {
                "current_version": FORGE_V2_CONTRACT_VERSION,
                "supported_versions": [FORGE_V2_CONTRACT_VERSION],
                "aliases": [],
            },
            "grace": {
                "identity": FORGE_V1_IDENTITY,
                "tool_count": len(grace_tools),
                "tool_names": [str(tool["name"]) for tool in grace_tools],
                "tool_surface_hash": _hash_json(grace_tools),
                "tool_hashes": _tool_hashes(grace_tools),
            },
        },
    }


__all__ = ["RELEASE_CONTRACT_VERSION", "build_release_contract"]
