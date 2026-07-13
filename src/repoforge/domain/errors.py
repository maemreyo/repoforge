"""Stable domain error taxonomy used across interfaces and application use cases."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ErrorCode(str, Enum):
    CONFIG_INVALID = "CONFIG_INVALID"
    CONFIG_STALE = "CONFIG_STALE"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    INPUT_REQUIRED = "INPUT_REQUIRED"
    SECURITY_POLICY_VIOLATION = "SECURITY_POLICY_VIOLATION"
    COMMAND_FAILED = "COMMAND_FAILED"
    COMMAND_TIMEOUT = "COMMAND_TIMEOUT"
    WORKSPACE_INVALID = "WORKSPACE_INVALID"
    STALE_STATE = "STALE_STATE"
    LOCK_TIMEOUT = "LOCK_TIMEOUT"
    RUNTIME_UNAVAILABLE = "RUNTIME_UNAVAILABLE"
    RUNTIME_RELOADING = "RUNTIME_RELOADING"
    RUNTIME_FAIL_CLOSED = "RUNTIME_FAIL_CLOSED"
    ALREADY_RUNNING = "ALREADY_RUNNING"
    ALREADY_EXISTS = "ALREADY_EXISTS"
    NOT_FOUND = "NOT_FOUND"
    INTERNAL_ERROR = "INTERNAL_ERROR"


@dataclass(frozen=True, slots=True)
class OperationError:
    code: ErrorCode
    what_happened: str
    why: str
    unchanged_state: tuple[str, ...] = ()
    safe_next_action: str = "Review the error and retry after correcting the reported condition."
    retryable: bool = False
    correlation_id: str | None = None


_PREFIX_CODES: tuple[tuple[str, ErrorCode, bool], ...] = (
    ("STALE_CONFIG", ErrorCode.CONFIG_STALE, True),
    ("STALE_ACTIVE", ErrorCode.CONFIG_STALE, True),
    ("STALE_ACTIVATION", ErrorCode.CONFIG_STALE, True),
    ("STALE_", ErrorCode.STALE_STATE, True),
    ("LOCK_TIMEOUT", ErrorCode.LOCK_TIMEOUT, True),
    ("RUNTIME_RELOADING", ErrorCode.RUNTIME_RELOADING, True),
    ("RUNTIME_FAIL_CLOSED", ErrorCode.RUNTIME_FAIL_CLOSED, False),
    ("RESTRICTIVE_ACTIVATION_FAILED", ErrorCode.RUNTIME_FAIL_CLOSED, False),
    ("RUNTIME_", ErrorCode.RUNTIME_UNAVAILABLE, True),
    ("ALREADY_RUNNING", ErrorCode.ALREADY_RUNNING, False),
    ("ALREADY_STARTING", ErrorCode.ALREADY_RUNNING, True),
    ("APPROVAL_REQUIRED", ErrorCode.APPROVAL_REQUIRED, False),
    ("ROLLBACK_APPROVAL_REQUIRED", ErrorCode.APPROVAL_REQUIRED, False),
    ("INPUT_REQUIRED", ErrorCode.INPUT_REQUIRED, False),
    ("COMMAND_TIMEOUT", ErrorCode.COMMAND_TIMEOUT, True),
)


class RepoForgeError(RuntimeError):
    default_code = ErrorCode.INTERNAL_ERROR

    def __init__(
        self,
        message: str,
        *,
        code: ErrorCode | None = None,
        retryable: bool | None = None,
        safe_next_action: str | None = None,
        unchanged_state: tuple[str, ...] = (),
        correlation_id: str | None = None,
    ) -> None:
        super().__init__(message)
        inferred_code, inferred_retryable = _infer_code(message, self.default_code)
        self.code = code or inferred_code
        self.retryable = inferred_retryable if retryable is None else retryable
        self.safe_next_action = safe_next_action
        self.unchanged_state = unchanged_state
        self.correlation_id = correlation_id


PersonalCodingMCPError = RepoForgeError


class ConfigError(RepoForgeError):
    default_code = ErrorCode.CONFIG_INVALID


class SecurityError(RepoForgeError):
    default_code = ErrorCode.SECURITY_POLICY_VIOLATION


class CommandError(RepoForgeError):
    default_code = ErrorCode.COMMAND_FAILED


class WorkspaceError(RepoForgeError):
    default_code = ErrorCode.WORKSPACE_INVALID


def _infer_code(message: str, default: ErrorCode) -> tuple[ErrorCode, bool]:
    upper = message.upper()
    for prefix, code, retryable in _PREFIX_CODES:
        if upper.startswith(prefix):
            return code, retryable
    if "TIMED OUT" in upper or "TIMEOUT" in upper:
        return (ErrorCode.COMMAND_TIMEOUT if default is ErrorCode.COMMAND_FAILED else default, True)
    if "UNKNOWN" in upper or "NOT FOUND" in upper or "MISSING" in upper:
        return ErrorCode.NOT_FOUND, False
    if "ALREADY EXISTS" in upper:
        return ErrorCode.ALREADY_EXISTS, False
    return default, False


def operation_error_from_exception(
    exc: BaseException, *, correlation_id: str | None = None
) -> OperationError:
    code = getattr(exc, "code", ErrorCode.INTERNAL_ERROR)
    if not isinstance(code, ErrorCode):
        try:
            code = ErrorCode(str(code))
        except ValueError:
            code = ErrorCode.INTERNAL_ERROR
    retryable = bool(getattr(exc, "retryable", False))
    unchanged = tuple(getattr(exc, "unchanged_state", ()))
    safe_action = getattr(exc, "safe_next_action", None) or (
        "Refresh the latest state and retry the same reviewed operation."
        if retryable
        else "Correct the reported invariant or provide the required explicit approval."
    )
    why = {
        ErrorCode.CONFIG_STALE: "Another writer changed the reviewed configuration first.",
        ErrorCode.STALE_STATE: "The optimistic-lock snapshot no longer matches current state.",
        ErrorCode.APPROVAL_REQUIRED: "The operation would widen capability without matching approval.",
        ErrorCode.LOCK_TIMEOUT: "Another process currently owns the required mutation lock.",
        ErrorCode.RUNTIME_RELOADING: "The runtime is draining and rejects new work until activation completes.",
        ErrorCode.RUNTIME_FAIL_CLOSED: "A restrictive transition failed and revoked capability remains blocked.",
        ErrorCode.ALREADY_RUNNING: "An identity-validated runtime already owns the supervisor lock.",
    }.get(code, "The requested operation did not satisfy a validated policy or runtime invariant.")
    return OperationError(
        code,
        str(exc),
        why,
        unchanged,
        safe_action,
        retryable,
        correlation_id or getattr(exc, "correlation_id", None),
    )
