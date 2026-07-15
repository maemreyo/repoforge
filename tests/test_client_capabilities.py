from __future__ import annotations

from typing import Any

import pytest
from mcp.server.fastmcp import Context, FastMCP
from mcp.shared.memory import create_connected_server_and_client_session
from mcp.types import ElicitRequestParams, ElicitResult, Implementation

from repoforge.application.capability_policy import CapabilityPolicy, SafeAction
from repoforge.domain.client_capabilities import (
    ClientFeature,
    parse_client_capabilities,
)
from repoforge.interfaces.mcp.capabilities import client_capabilities_from_context


def _full_initialization() -> dict[str, object]:
    return {
        "protocolVersion": "2025-11-25",
        "clientInfo": {"name": "test-client", "version": "3.2.1"},
        "capabilities": {
            "elicitation": {"form": {}, "url": {}},
            "tasks": {"list": {}, "cancel": {}, "requests": {"elicitation": {"create": {}}}},
            "experimental": {
                "io.modelcontextprotocol/ui": {"version": "1.0"},
                "io.modelcontextprotocol/tool-search": {
                    "version": "2026-01",
                    "deferredDiscovery": True,
                },
                "repoforge": {
                    "progressNotifications": True,
                    "cancellationNotifications": True,
                    "resourceSubscriptions": True,
                    "compatibilityFlags": ["chatgpt-actions-v2", "strict-json-results"],
                },
            },
        },
    }


def test_client_capabilities_normalize_standard_and_extension_features() -> None:
    capabilities = parse_client_capabilities(_full_initialization())

    assert capabilities.protocol_version == "2025-11-25"
    assert capabilities.client_name == "test-client"
    assert capabilities.client_version == "3.2.1"
    assert capabilities.supports(ClientFeature.APPS_UI)
    assert capabilities.supports(ClientFeature.ELICITATION_FORM)
    assert capabilities.supports(ClientFeature.ELICITATION_URL)
    assert capabilities.supports(ClientFeature.TASKS)
    assert capabilities.supports(ClientFeature.PROGRESS_NOTIFICATIONS)
    assert capabilities.supports(ClientFeature.CANCELLATION_NOTIFICATIONS)
    assert capabilities.supports(ClientFeature.TOOL_SEARCH)
    assert capabilities.supports(ClientFeature.DEFERRED_DISCOVERY)
    assert capabilities.supports(ClientFeature.RESOURCE_SUBSCRIPTIONS)
    assert capabilities.feature(ClientFeature.APPS_UI).version == "1.0"
    assert capabilities.feature(ClientFeature.TOOL_SEARCH).version == "2026-01"
    assert capabilities.compatibility_flags == (
        "chatgpt-actions-v2",
        "strict-json-results",
    )
    assert capabilities.malformed_fields == ()
    assert capabilities.legacy is False


def test_client_capabilities_fail_closed_for_empty_malformed_and_legacy_clients() -> None:
    empty = parse_client_capabilities(
        {
            "protocolVersion": "2025-11-25",
            "clientInfo": {"name": "empty", "version": "1"},
            "capabilities": {},
        }
    )
    malformed = parse_client_capabilities(
        {
            "protocolVersion": "2025-11-25",
            "clientInfo": "not-an-object",
            "capabilities": ["elicitation"],
        }
    )
    legacy = parse_client_capabilities(None)

    for normalized in (empty, malformed, legacy):
        assert all(not normalized.supports(feature) for feature in ClientFeature)
    assert empty.malformed_fields == ()
    assert malformed.malformed_fields == ("capabilities", "clientInfo")
    assert malformed.client_name == "unknown"
    assert legacy.legacy is True
    assert legacy.protocol_version == "legacy"
    assert parse_client_capabilities(None).as_dict() == legacy.as_dict()


def test_partial_elicitation_support_does_not_enable_other_modes() -> None:
    capabilities = parse_client_capabilities(
        {
            "protocolVersion": "2025-11-25",
            "clientInfo": {"name": "forms-only", "version": "1"},
            "capabilities": {"elicitation": {"form": {}}},
        }
    )

    assert capabilities.supports(ClientFeature.ELICITATION_FORM)
    assert not capabilities.supports(ClientFeature.ELICITATION_URL)
    assert not capabilities.supports(ClientFeature.TASKS)


def test_capability_policy_returns_complete_structured_fallbacks() -> None:
    policy = CapabilityPolicy(parse_client_capabilities(None))

    app = policy.present_app_or_fallback(
        summary="Review the proposed repository policy.",
        actions=(
            SafeAction("approve-policy", "Approve policy", required=True),
            SafeAction("edit-policy", "Edit policy", required=False),
        ),
    )
    assert app == {
        "delivery": "structured",
        "fallback_for": "apps_ui",
        "summary": "Review the proposed repository policy.",
        "actions": [
            {"action_id": "approve-policy", "label": "Approve policy", "required": True},
            {"action_id": "edit-policy", "label": "Edit policy", "required": False},
        ],
    }

    elicitation = policy.input_required(
        decision_id="base-branch",
        prompt="Choose the base branch.",
        allowed_options=("main", "release"),
    )
    assert elicitation == {
        "status": "INPUT_REQUIRED",
        "fallback_for": "elicitation",
        "decision_id": "base-branch",
        "prompt": "Choose the base branch.",
        "allowed_options": ["main", "release"],
    }

    task = policy.deliver_task("op-0123456789abcdef01234567", cancel_supported=True)
    assert task == {
        "delivery": "repoforge_operation",
        "fallback_for": "tasks",
        "operation_id": "op-0123456789abcdef01234567",
        "status_tool": "operation_status",
        "cancel_tool": "operation_cancel",
    }

    tools = policy.discover_tools(("repo_list", "repo_context", "workspace_create"))
    assert tools == {
        "delivery": "static",
        "fallback_for": "tool_search",
        "tools": ["repo_list", "repo_context", "workspace_create"],
        "complete": True,
    }

    progress = policy.deliver_progress("op-0123456789abcdef01234567")
    assert progress == {
        "delivery": "polling",
        "fallback_for": "progress_notifications",
        "operation_id": "op-0123456789abcdef01234567",
        "status_tool": "operation_status",
    }


def test_capability_policy_never_allows_an_unnegotiated_request() -> None:
    form_only = parse_client_capabilities(
        {
            "protocolVersion": "2025-11-25",
            "clientInfo": {"name": "forms-only", "version": "1"},
            "capabilities": {"elicitation": {"form": {}}},
        }
    )
    policy = CapabilityPolicy(form_only)

    assert policy.may_emit(ClientFeature.ELICITATION_FORM)
    assert not policy.may_emit(ClientFeature.ELICITATION_URL)
    assert not policy.may_emit(ClientFeature.APPS_UI)
    assert policy.decision(ClientFeature.ELICITATION_URL).fallback == "input_required"
    assert policy.decision(ClientFeature.APPS_UI).fallback == "structured"


@pytest.mark.anyio
async def test_capabilities_are_captured_from_each_mcp_connection() -> None:
    server = FastMCP("capability-test")

    @server.tool(structured_output=True)
    def negotiated(ctx: Context) -> dict[str, Any]:
        return client_capabilities_from_context(ctx).as_dict()

    async with create_connected_server_and_client_session(server) as session:
        result = await session.call_tool("negotiated", {})
        assert result.isError is False
        assert result.structuredContent is not None
        assert result.structuredContent["features"]["elicitation_form"]["supported"] is False
        assert result.structuredContent["features"]["elicitation_url"]["supported"] is False

    async def elicitation_callback(
        _context: Any,
        _params: ElicitRequestParams,
    ) -> ElicitResult:
        return ElicitResult(action="cancel")

    async with create_connected_server_and_client_session(
        server,
        elicitation_callback=elicitation_callback,
        client_info=Implementation(name="capable-client", version="9.0"),
    ) as session:
        result = await session.call_tool("negotiated", {})
        assert result.isError is False
        assert result.structuredContent is not None
        assert result.structuredContent["client_name"] == "capable-client"
        assert result.structuredContent["features"]["elicitation_form"]["supported"] is True
        assert result.structuredContent["features"]["elicitation_url"]["supported"] is True
