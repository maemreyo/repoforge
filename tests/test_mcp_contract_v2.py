from __future__ import annotations

import json
from typing import Any

import pytest
from conftest import ForgeEnvironment
from mcp.shared.memory import create_connected_server_and_client_session
from pydantic import ValidationError

from repoforge.contracts.registry import V2_TOOL_NAMES, V2_TOOL_SPECS
from repoforge.interfaces.mcp.grace import (
    FORGE_V1_IDENTITY,
    create_grace_server,
)
from repoforge.interfaces.mcp.server import (
    FORGE_V2_IDENTITY,
    create_server,
    tool_surface_hash,
)


@pytest.mark.anyio
async def test_forge_v2_identity_publishes_exact_authoritative_roster_and_schemas(
    forge_env: ForgeEnvironment,
) -> None:
    server = create_server(service=forge_env.service)

    assert server.name == FORGE_V2_IDENTITY == "forge_v2"
    tools = await server.list_tools()
    assert tuple(tool.name for tool in tools) == V2_TOOL_NAMES
    assert len(tools) == 28
    assert {"repo_status", "workspace_run_profile", "repo_policy_apply"}.isdisjoint(
        tool.name for tool in tools
    )

    for tool in tools:
        spec = V2_TOOL_SPECS[tool.name]
        assert tool.inputSchema == spec.input_model.model_json_schema(mode="validation")
        assert tool.outputSchema == spec.output_model.model_json_schema(mode="validation")
        assert tool.annotations is not None


@pytest.mark.anyio
async def test_protocol_rejects_undeclared_input_before_dispatch(
    forge_env: ForgeEnvironment,
) -> None:
    server = create_server(service=forge_env.service)
    async with create_connected_server_and_client_session(server) as session:
        result = await session.call_tool("repo_list", {"undeclared": True})

    assert result.isError is True
    rendered = "\n".join(getattr(item, "text", "") for item in result.content)
    assert "extra_forbidden" in rendered or "Extra inputs are not permitted" in rendered
    assert result.structuredContent is not None
    assert result.structuredContent["status"] == "failed"


@pytest.mark.anyio
async def test_protocol_validates_output_against_authoritative_model() -> None:
    class InvalidOutputService:
        config: Any = None
        metrics: Any = None

        def repo_list_v2(self, **_: object) -> dict[str, object]:
            return {"status": "ok", "unexpected": True}

    server = create_server(service=InvalidOutputService())  # type: ignore[arg-type]
    async with create_connected_server_and_client_session(server) as session:
        result = await session.call_tool("repo_list", {})

    assert result.isError is True
    rendered = "\n".join(getattr(item, "text", "") for item in result.content)
    assert "unexpected" in rendered
    assert "extra_forbidden" in rendered or "Extra inputs are not permitted" in rendered
    assert result.structuredContent is not None
    assert result.structuredContent["status"] == "failed"


@pytest.mark.anyio
async def test_protocol_error_is_one_redacted_typed_envelope(
    forge_env: ForgeEnvironment,
) -> None:
    server = create_server(service=forge_env.service)
    async with create_connected_server_and_client_session(server) as session:
        result = await session.call_tool(
            "repo_read",
            {"repo_id": "missing", "files": [{"path": "README.md"}]},
        )

    assert result.isError is True
    rendered = "\n".join(getattr(item, "text", "") for item in result.content)
    envelope = json.loads(rendered)
    assert set(envelope) >= {
        "status",
        "error_code",
        "what_happened",
        "why",
        "correlation_id",
        "safe_next_action",
        "retryable",
    }
    assert envelope["status"] == "failed"
    # The typed error envelope must be real structuredContent, not only
    # recoverable by parsing JSON out of the text block (#225 review).
    assert result.structuredContent == envelope


def test_surface_hash_is_v2_identity_bound_and_version_negotiation_is_gone() -> None:
    assert tool_surface_hash() == tool_surface_hash()
    assert len(tool_surface_hash()) == 64
    with pytest.raises(ValueError, match="only supports contract v2"):
        tool_surface_hash(1)


@pytest.mark.anyio
async def test_retired_forge_v1_identity_exposes_one_typed_grace_error() -> None:
    stale_calls: list[dict[str, object]] = []
    shutdown_requests: list[bool] = []
    server = create_grace_server(
        on_stale_caller=stale_calls.append,
        request_shutdown=lambda: shutdown_requests.append(True),
    )

    assert server.name == FORGE_V1_IDENTITY == "forge_v1"
    tools = await server.list_tools()
    assert [tool.name for tool in tools] == ["migration_required"]

    async with create_connected_server_and_client_session(server) as session:
        result = await session.call_tool(
            "migration_required",
            {"reported_surface_hash": "stale-surface"},
        )

    assert result.isError is False
    payload = result.structuredContent
    assert payload is not None
    assert payload["status"] == "failed"
    assert payload["error_code"] == "CONNECTOR_RETIRED"
    assert payload["retired_identity"] == FORGE_V1_IDENTITY
    assert payload["new_identity"] == FORGE_V2_IDENTITY
    assert payload["expected_surface_hash"] == tool_surface_hash()
    assert payload["reported_surface_hash"] == "stale-surface"
    assert payload["shutdown_required"] is True
    assert stale_calls and stale_calls[0]["surface_mismatch"] is True
    assert shutdown_requests == [True]


def test_authoritative_models_still_fail_closed_independently() -> None:
    with pytest.raises(ValidationError):
        V2_TOOL_SPECS["repo_list"].validate_input({"undeclared": True})
