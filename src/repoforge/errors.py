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
    operation_error_from_exception,
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
    "operation_error_from_exception",
]
