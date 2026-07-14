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
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    IDEMPOTENCY_IN_PROGRESS = "IDEMPOTENCY_IN_PROGRESS"
    STATE_PERSISTENCE_FAILED = "STATE_PERSISTENCE_FAILED"
    NOT_FOUND = "NOT_FOUND"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    DISCOVERY_ROOT_NOT_FOUND = "DISCOVERY_ROOT_NOT_FOUND"
    DISCOVERY_PERMISSION_DENIED = "DISCOVERY_PERMISSION_DENIED"
    DUPLICATE_REPOSITORY_ID = "DUPLICATE_REPOSITORY_ID"
    INTERACTION_REQUIRED = "INTERACTION_REQUIRED"
    SESSION_NOT_FOUND = "SESSION_NOT_FOUND"
    SESSION_CORRUPT = "SESSION_CORRUPT"
    SESSION_STALE = "SESSION_STALE"
    CONFIG_CHANGED = "CONFIG_CHANGED"
    REPOSITORY_FACTS_CHANGED = "REPOSITORY_FACTS_CHANGED"
    PROPOSAL_BLOCKED = "PROPOSAL_BLOCKED"
    DECISION_REQUIRED = "DECISION_REQUIRED"
    APPROVAL_MISMATCH = "APPROVAL_MISMATCH"
    CANDIDATE_SMOKE_FAILED = "CANDIDATE_SMOKE_FAILED"
    ACTIVATION_FAILED = "ACTIVATION_FAILED"
    EXECUTABLE_SHADOWED = "EXECUTABLE_SHADOWED"
    REPOSITORY_REF_NOT_FOUND = "REPOSITORY_REF_NOT_FOUND"
    REPOSITORY_REF_AMBIGUOUS = "REPOSITORY_REF_AMBIGUOUS"
    REPOSITORY_REF_EXTERNAL = "REPOSITORY_REF_EXTERNAL"
    REPOSITORY_REF_DISALLOWED = "REPOSITORY_REF_DISALLOWED"
    REPOSITORY_HISTORIES_UNRELATED = "REPOSITORY_HISTORIES_UNRELATED"
    REPOSITORY_HISTORY_INCOMPLETE = "REPOSITORY_HISTORY_INCOMPLETE"
    REPOSITORY_EVIDENCE_LIMIT_INVALID = "REPOSITORY_EVIDENCE_LIMIT_INVALID"
    REPOSITORY_EVIDENCE_PARSE_FAILED = "REPOSITORY_EVIDENCE_PARSE_FAILED"
    CHECK_SELECTOR_INVALID = "CHECK_SELECTOR_INVALID"
    CHECK_EVIDENCE_STALE = "CHECK_EVIDENCE_STALE"
    CHECK_EVIDENCE_UNAVAILABLE = "CHECK_EVIDENCE_UNAVAILABLE"
    OPERATION_INVALID = "OPERATION_INVALID"
    OPERATION_NOT_FOUND = "OPERATION_NOT_FOUND"
    OPERATION_STALE = "OPERATION_STALE"
    OPERATION_CORRUPT = "OPERATION_CORRUPT"
    OPERATION_SCHEMA_UNSUPPORTED = "OPERATION_SCHEMA_UNSUPPORTED"
    OPERATION_TRANSITION_INVALID = "OPERATION_TRANSITION_INVALID"
    STALE_ASSESSMENT_SNAPSHOT = "STALE_ASSESSMENT_SNAPSHOT"
    ASSESSMENT_COMPONENT_UNAVAILABLE = "ASSESSMENT_COMPONENT_UNAVAILABLE"
    ASSESSMENT_INVALID = "ASSESSMENT_INVALID"
    DIAGNOSTIC_NOT_FOUND = "DIAGNOSTIC_NOT_FOUND"
    DIAGNOSTIC_SELECTOR_REQUIRED = "DIAGNOSTIC_SELECTOR_REQUIRED"
    DIAGNOSTIC_SELECTOR_INVALID = "DIAGNOSTIC_SELECTOR_INVALID"
    DIAGNOSTIC_STALE_WORKSPACE = "DIAGNOSTIC_STALE_WORKSPACE"
    DIAGNOSTIC_TOOL_MISSING = "DIAGNOSTIC_TOOL_MISSING"
    DIAGNOSTIC_TIMEOUT = "DIAGNOSTIC_TIMEOUT"
    DIAGNOSTIC_PARSER_FAILED = "DIAGNOSTIC_PARSER_FAILED"
    DIAGNOSTIC_UNEXPECTED_MUTATION = "DIAGNOSTIC_UNEXPECTED_MUTATION"
    DIAGNOSTIC_OUTPUT_INVALID = "DIAGNOSTIC_OUTPUT_INVALID"


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
    ("DISCOVERY_ROOT_NOT_FOUND", ErrorCode.DISCOVERY_ROOT_NOT_FOUND, False),
    ("DISCOVERY_PERMISSION_DENIED", ErrorCode.DISCOVERY_PERMISSION_DENIED, False),
    ("DUPLICATE_REPOSITORY_ID", ErrorCode.DUPLICATE_REPOSITORY_ID, False),
    ("INTERACTION_REQUIRED", ErrorCode.INTERACTION_REQUIRED, False),
    ("SESSION_NOT_FOUND", ErrorCode.SESSION_NOT_FOUND, False),
    ("SESSION_CORRUPT", ErrorCode.SESSION_CORRUPT, False),
    ("SESSION_STALE", ErrorCode.SESSION_STALE, True),
    ("CONFIG_CHANGED", ErrorCode.CONFIG_CHANGED, True),
    ("REPOSITORY_FACTS_CHANGED", ErrorCode.REPOSITORY_FACTS_CHANGED, False),
    ("PROPOSAL_BLOCKED", ErrorCode.PROPOSAL_BLOCKED, False),
    ("DECISION_REQUIRED", ErrorCode.DECISION_REQUIRED, False),
    ("APPROVAL_MISMATCH", ErrorCode.APPROVAL_MISMATCH, False),
    ("CANDIDATE_SMOKE_FAILED", ErrorCode.CANDIDATE_SMOKE_FAILED, False),
    ("ACTIVATION_FAILED", ErrorCode.ACTIVATION_FAILED, True),
    ("EXECUTABLE_SHADOWED", ErrorCode.EXECUTABLE_SHADOWED, False),
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
    ("IDEMPOTENCY_CONFLICT", ErrorCode.IDEMPOTENCY_CONFLICT, False),
    ("IDEMPOTENCY_IN_PROGRESS", ErrorCode.IDEMPOTENCY_IN_PROGRESS, True),
    ("STATE_PERSISTENCE_FAILED", ErrorCode.STATE_PERSISTENCE_FAILED, True),
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
        ErrorCode.IDEMPOTENCY_CONFLICT: "The same idempotency key was already bound to different reviewed input.",
        ErrorCode.IDEMPOTENCY_IN_PROGRESS: "Another process is still executing the same keyed operation.",
        ErrorCode.STATE_PERSISTENCE_FAILED: "RepoForge could not durably record required local operational state.",
        ErrorCode.REPOSITORY_REF_NOT_FOUND: "The requested immutable Git ref does not resolve to a committed snapshot.",
        ErrorCode.REPOSITORY_REF_AMBIGUOUS: "Abbreviated Git object names are not accepted for snapshot reads.",
        ErrorCode.REPOSITORY_REF_EXTERNAL: "The requested ref is outside the reviewed local base-branch history.",
        ErrorCode.REPOSITORY_REF_DISALLOWED: "The requested ref form is not permitted by repository read policy.",
        ErrorCode.REPOSITORY_HISTORIES_UNRELATED: "The two reviewed commits have no merge base and cannot be compared as one history.",
        ErrorCode.REPOSITORY_HISTORY_INCOMPLETE: "The local clone lacks enough committed history to calculate the requested evidence.",
        ErrorCode.REPOSITORY_EVIDENCE_LIMIT_INVALID: "The requested committed-evidence limit is outside the reviewed bound.",
        ErrorCode.REPOSITORY_EVIDENCE_PARSE_FAILED: "Git returned committed evidence that did not match the typed parser contract.",
        ErrorCode.CHECK_SELECTOR_INVALID: "The supplied value is not an opaque CI selector issued by RepoForge.",
        ErrorCode.CHECK_EVIDENCE_STALE: "The selected Check Run does not match the exact pushed workspace commit.",
        ErrorCode.CHECK_EVIDENCE_UNAVAILABLE: "GitHub did not return the primary Check Run evidence required for this read.",
        ErrorCode.OPERATION_INVALID: "The operation request violates a typed identity, progress, scope, or bounds invariant.",
        ErrorCode.OPERATION_NOT_FOUND: "No durable operation exists for the supplied identifier.",
        ErrorCode.OPERATION_STALE: "Another writer changed the durable operation after the caller's reviewed timestamp.",
        ErrorCode.OPERATION_CORRUPT: "The persisted operation record is malformed, unsafe, or inconsistent with its identity.",
        ErrorCode.OPERATION_SCHEMA_UNSUPPORTED: "The operation record uses a schema version this RepoForge build cannot safely interpret.",
        ErrorCode.OPERATION_TRANSITION_INVALID: "The requested state transition is not allowed by the durable operation state machine.",
        ErrorCode.STALE_ASSESSMENT_SNAPSHOT: "The workspace, configuration, or policy identity changed while evidence was being collected.",
        ErrorCode.ASSESSMENT_COMPONENT_UNAVAILABLE: "A bounded assessment provider could not return trustworthy evidence for the captured snapshot.",
        ErrorCode.ASSESSMENT_INVALID: "The assessment model violates snapshot identity, coverage, ordering, or bound invariants.",
        ErrorCode.DIAGNOSTIC_NOT_FOUND: "The requested diagnostic is not part of the reviewed repository capability set.",
        ErrorCode.DIAGNOSTIC_SELECTOR_REQUIRED: "The diagnostic requires one typed selector before its reviewed argv can be resolved.",
        ErrorCode.DIAGNOSTIC_SELECTOR_INVALID: "The supplied selector violates the configured type, path policy, or closed value set.",
        ErrorCode.DIAGNOSTIC_STALE_WORKSPACE: "The workspace fingerprint changed after the caller reviewed it.",
        ErrorCode.DIAGNOSTIC_TOOL_MISSING: "The reviewed diagnostic executable is not available in the constrained runtime path.",
        ErrorCode.DIAGNOSTIC_TIMEOUT: "The reviewed diagnostic exceeded its configured bounded execution time.",
        ErrorCode.DIAGNOSTIC_PARSER_FAILED: "The configured parser could not interpret the bounded diagnostic output safely.",
        ErrorCode.DIAGNOSTIC_UNEXPECTED_MUTATION: "The diagnostic changed workspace paths outside its reviewed mutability contract.",
        ErrorCode.DIAGNOSTIC_OUTPUT_INVALID: "The diagnostic returned malformed or unsupported bounded output.",
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
