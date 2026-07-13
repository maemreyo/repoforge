"""Stable domain error taxonomy used across interfaces and application use cases."""

from __future__ import annotations
from dataclasses import dataclass
from enum import Enum


class ErrorCode(str, Enum):
    CONFIG_INVALID = "CONFIG_INVALID"
    SECURITY_POLICY_VIOLATION = "SECURITY_POLICY_VIOLATION"
    COMMAND_FAILED = "COMMAND_FAILED"
    WORKSPACE_INVALID = "WORKSPACE_INVALID"
    STALE_STATE = "STALE_STATE"
    INPUT_REQUIRED = "INPUT_REQUIRED"
    ALREADY_EXISTS = "ALREADY_EXISTS"
    NOT_FOUND = "NOT_FOUND"
    INTERNAL_ERROR = "INTERNAL_ERROR"


@dataclass(frozen=True, slots=True)
class OperationError:
    code: ErrorCode
    what_happened: str
    why: str
    unchanged_state: tuple[str, ...] = ()
    safe_next_action: str = (
        "Review the error and retry after correcting the reported condition."
    )
    retryable: bool = False
    correlation_id: str | None = None


class RepoForgeError(RuntimeError):
    code = ErrorCode.INTERNAL_ERROR


PersonalCodingMCPError = RepoForgeError


class ConfigError(RepoForgeError):
    code = ErrorCode.CONFIG_INVALID


class SecurityError(RepoForgeError):
    code = ErrorCode.SECURITY_POLICY_VIOLATION


class CommandError(RepoForgeError):
    code = ErrorCode.COMMAND_FAILED


class WorkspaceError(RepoForgeError):
    code = ErrorCode.WORKSPACE_INVALID
