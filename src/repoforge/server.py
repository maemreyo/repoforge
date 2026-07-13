"""Backward-compatible MCP interface imports."""

from .interfaces.mcp.server import (
    EXTERNAL_READ,
    EXTERNAL_WRITE,
    LOCAL_CREATE,
    LOCAL_DESTRUCTIVE,
    LOCAL_MUTATE,
    READ_ONLY,
    SERVER_INSTRUCTIONS,
    create_server,
    tool_surface_hash,
)

__all__ = [
    "EXTERNAL_READ",
    "EXTERNAL_WRITE",
    "LOCAL_CREATE",
    "LOCAL_DESTRUCTIVE",
    "LOCAL_MUTATE",
    "READ_ONLY",
    "SERVER_INSTRUCTIONS",
    "create_server",
    "tool_surface_hash",
]
