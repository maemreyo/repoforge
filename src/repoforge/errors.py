"""Backward-compatible error imports; canonical definitions live in domain.errors."""

from .domain.errors import (
    CommandError,
    ConfigError,
    ErrorCode,
    OperationError,
    PersonalCodingMCPError,
    RepoForgeError,
    SecurityError,
    WorkspaceError,
)

__all__ = [
    "CommandError",
    "ConfigError",
    "ErrorCode",
    "OperationError",
    "PersonalCodingMCPError",
    "RepoForgeError",
    "SecurityError",
    "WorkspaceError",
]
