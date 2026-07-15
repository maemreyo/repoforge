"""MCP adapter for connection-scoped client capability negotiation."""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import Context

from ...application.capability_policy import CapabilityPolicy
from ...domain.client_capabilities import ClientCapabilities, parse_client_capabilities


def client_capabilities_from_context(context: Context[Any, Any, Any]) -> ClientCapabilities:
    """Capture the current connection's initialize parameters without persisting them."""

    initialization = getattr(context.session, "client_params", None)
    return parse_client_capabilities(initialization)


def capability_policy_from_context(context: Context[Any, Any, Any]) -> CapabilityPolicy:
    """Build a request-local emission policy from the negotiated capability snapshot."""

    return CapabilityPolicy(client_capabilities_from_context(context))
