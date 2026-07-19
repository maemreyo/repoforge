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


def _failure_payload() -> dict[str, object]:
    return {
        "status": "failed",
        "summary": "Request failed",
        "error": {
            "code": "NOT_FOUND",
            "message": "Repository not found",
            "why": "The repository id is not enrolled.",
            "retryable": False,
            "safe_next_action": "Choose an enrolled repository.",
            "details": {"correlation_id": "corr-1"},
            "unchanged_state": ["No state changed."],
            "automatic_retry_allowed": False,
        },
    }


def test_every_advertised_output_schema_accepts_shared_failure() -> None:
    for spec in V2_TOOL_SPECS.values():
        validated = spec.validate_output(_failure_payload())
        assert validated.status == "failed"
        assert "anyOf" in spec.output_schema()


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
        assert tool.outputSchema == spec.output_schema()
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
    assert envelope["status"] == "failed"
    assert isinstance(envelope["summary"], str) and envelope["summary"]
    error = envelope["error"]
    assert set(error) >= {
        "code",
        "message",
        "why",
        "retryable",
        "safe_next_action",
        "details",
        "unchanged_state",
        "automatic_retry_allowed",
    }
    assert (
        isinstance(error["details"]["correlation_id"], str) and error["details"]["correlation_id"]
    )
    # The typed error envelope must be real structuredContent, not only
    # recoverable by parsing JSON out of the text block (#225 review), and it
    # must conform to the same {status, summary, error: ToolError} contract
    # every one of the 28 tools' own output model inherits from ToolResponse
    # -- not an ad-hoc shape a client cannot validate against any advertised
    # output schema.
    assert result.structuredContent == envelope
    validated = V2_TOOL_SPECS["repo_read"].validate_output(envelope)
    assert validated.status == "failed"


@pytest.mark.anyio
async def test_protocol_operation_wait_returns_typed_terminal_evidence(
    forge_env: ForgeEnvironment,
) -> None:
    manager = forge_env.service.operations
    task = manager.create(kind="watch", phase="queued", cancel_supported=False)
    running = manager.start(task.operation_id)
    manager.succeed(task.operation_id, result_reference="watch:done")
    server = create_server(service=forge_env.service)

    async with create_connected_server_and_client_session(server) as session:
        result = await session.call_tool(
            "operation",
            {
                "action": "wait",
                "operation_id": task.operation_id,
                "since_updated_at": running.updated_at,
                "timeout_seconds": 60,
            },
        )

    assert result.isError is False
    payload = result.structuredContent
    assert payload is not None
    assert payload["action"] == "wait"
    assert payload["changed_since"] is True
    assert payload["timed_out"] is False
    operation = payload["operation"]
    assert operation["terminal"] is True
    assert operation["suggested_poll_after_s"] is None
    assert operation["eta_seconds"] == 0.0
    V2_TOOL_SPECS["operation"].validate_output(payload)


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
