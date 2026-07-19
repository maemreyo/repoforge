"""Structured, bounded, secret-safe execution failure evidence and recovery choices."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any, NoReturn

from .egress import (
    EgressContentClass,
    EgressDestination,
    EgressPolicy,
    EgressRequest,
    evaluate_egress,
)
from .errors import ErrorCode, RepoForgeError
from .execution_receipt import WorkspaceIdentity

FAILURE_EVIDENCE_SCHEMA_VERSION = 1
_MAX_EXCERPT_CHARS = 4_000
_MAX_DIAGNOSTIC_CHARS = 500
_MAX_SCOPE_ITEMS = 100
_FAILURE_ID = re.compile(r"^failure-[a-f0-9]{24}$")
_OPERATION_ID = re.compile(r"^op-[a-f0-9]{24}$")
_PLAN_ID = re.compile(r"^plan-[a-f0-9]{24}$")
_RECEIPT_ID = re.compile(r"^receipt-[a-f0-9]{24}$")
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")
_SHA64 = re.compile(r"^[a-f0-9]{64}$")
_GIT_SHA = re.compile(r"^(?:[a-f0-9]{40}|[a-f0-9]{64})$")


class FailureClass(str, Enum):
    TOOL_MISSING = "tool_missing"
    DEPENDENCY_MISSING = "dependency_missing"
    ENVIRONMENT_MISMATCH = "environment_mismatch"
    CONFIGURATION_INVALID = "configuration_invalid"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    LINT_FAILURE = "lint_failure"
    TYPE_FAILURE = "type_failure"
    TEST_FAILURE = "test_failure"
    BUILD_FAILURE = "build_failure"
    NETWORK_FAILURE = "network_failure"
    PERMISSION_FAILURE = "permission_failure"
    POLICY_FAILURE = "policy_failure"
    STALE_WORKSPACE = "stale_workspace"
    STALE_PLAN = "stale_plan"
    UNEXPECTED_MUTATION = "unexpected_mutation"
    PROVIDER_FAILURE = "provider_failure"
    FLAKY_SUSPECTED = "flaky_suspected"
    UNKNOWN = "unknown"


FAILURE_CLASSES: tuple[str, ...] = tuple(item.value for item in FailureClass)


class FailureReproducibility(str, Enum):
    REPRODUCIBLE = "reproducible"
    INTERMITTENT = "intermittent"
    UNKNOWN = "unknown"


class RecoveryActionKind(str, Enum):
    """The exact Forge v2 tool a recovery action names.

    RepoForge exposes exactly 28 static tools (#180); several v1-era
    operations (`workspace_run_diagnostic`, `workspace_run_profile`,
    `workspace_create_execution_plan`, `workspace_execute_plan`,
    `workspace_refresh_preview`, `workspace_restore_paths`,
    `operation_status`) no longer exist as standalone tools. `kind` here is
    always a real, currently-callable tool name; `RecoveryAction.mode` and
    `.plan_action` carry the sub-mode a client needs to reconstruct the exact
    call for tools that were consolidated behind a mode/action field."""

    OPERATION = "operation"
    WORKSPACE_STATUS = "workspace_status"
    WORKSPACE_VERIFY = "workspace_verify"
    WORKSPACE_REFRESH = "workspace_refresh"
    WORKSPACE_MUTATE = "workspace_mutate"
    CONFIG_INSPECT = "config_inspect"


_KINDS_REQUIRING_WORKSPACE_ID = frozenset(
    {
        RecoveryActionKind.WORKSPACE_STATUS,
        RecoveryActionKind.WORKSPACE_VERIFY,
        RecoveryActionKind.WORKSPACE_REFRESH,
        RecoveryActionKind.WORKSPACE_MUTATE,
    }
)


@dataclass(frozen=True, slots=True)
class RecoveryAction:
    """Every field here names a real field on the real Forge v2 tool Input
    model for `kind` (verified against `contracts/v2.py`), so a caller can
    reconstruct the exact tool call rather than guess or re-derive parameters
    from context. `action` carries `OperationInput.action` for `operation`
    (e.g. "get") and `WorkspaceRefreshInput.action` for `workspace_refresh`
    (e.g. "preview"/"apply") -- two different tools' fields that happen to
    share a name; which one applies is determined by `kind`."""

    kind: RecoveryActionKind
    precondition: str
    workspace_id: str | None = None
    mode: str | None = None
    plan_action: str | None = None
    diagnostic_id: str | None = None
    profile_name: str | None = None
    plan_through: str | None = None
    relative_paths: tuple[str, ...] = ()
    operation_id: str | None = None
    plan_id: str | None = None
    action: str | None = None
    expected_head_sha: str | None = None
    expected_workspace_fingerprint: str | None = None

    def __post_init__(self) -> None:
        _safe_text(self.precondition, "recovery action precondition", 500)
        for field, value in (
            ("diagnostic_id", self.diagnostic_id),
            ("profile_name", self.profile_name),
        ):
            if value is not None and _SAFE_ID.fullmatch(value) is None:
                _invalid(f"Recovery action {field} is invalid")
        if self.workspace_id is not None and _SAFE_ID.fullmatch(self.workspace_id) is None:
            _invalid("Recovery action workspace_id is invalid")
        if self.kind in _KINDS_REQUIRING_WORKSPACE_ID and self.workspace_id is None:
            _invalid(f"{self.kind.value} recovery action requires workspace_id")
        if self.kind not in _KINDS_REQUIRING_WORKSPACE_ID and self.workspace_id is not None:
            _invalid(f"workspace_id is not valid for {self.kind.value} recovery actions")
        if self.operation_id is not None and _OPERATION_ID.fullmatch(self.operation_id) is None:
            _invalid("Recovery action operation_id is invalid")
        if self.plan_id is not None and _PLAN_ID.fullmatch(self.plan_id) is None:
            _invalid("Recovery action plan_id is invalid")
        if self.plan_through is not None and self.plan_through not in {"iteration", "full"}:
            _invalid("Recovery action plan_through boundary is invalid")
        if self.mode is not None and self.mode not in {"auto", "diagnostic", "profile", "plan"}:
            _invalid("Recovery action mode is invalid")
        if self.plan_action is not None and self.plan_action not in {
            "create",
            "accept",
            "execute",
        }:
            _invalid("Recovery action plan_action is invalid")
        if (
            self.expected_head_sha is not None
            and _GIT_SHA.fullmatch(self.expected_head_sha) is None
        ):
            _invalid("Recovery action expected_head_sha is invalid")
        if (
            self.expected_workspace_fingerprint is not None
            and _SHA64.fullmatch(self.expected_workspace_fingerprint) is None
        ):
            _invalid("Recovery action expected_workspace_fingerprint is invalid")
        object.__setattr__(
            self,
            "relative_paths",
            _safe_paths(self.relative_paths, "recovery action relative_paths"),
        )
        verify_only = (self.mode, self.plan_action, self.diagnostic_id, self.profile_name)
        if self.kind is not RecoveryActionKind.WORKSPACE_VERIFY and (
            any(value is not None for value in verify_only) or self.plan_through is not None
        ):
            _invalid(
                "mode, plan_action, diagnostic_id, profile_name, and plan_through are only "
                "valid for workspace_verify recovery actions"
            )
        if self.kind is RecoveryActionKind.WORKSPACE_VERIFY:
            if self.mode is None:
                _invalid("workspace_verify recovery action requires mode")
            if self.mode == "diagnostic" and self.diagnostic_id is None:
                _invalid("Diagnostic recovery action requires diagnostic_id")
            if self.mode == "profile" and self.profile_name is None:
                _invalid("Profile recovery action requires profile_name")
            if self.mode == "plan":
                if self.plan_action is None:
                    _invalid("Plan recovery action requires plan_action")
                if self.plan_action == "execute" and self.plan_through is None:
                    _invalid("Plan execute recovery action requires plan_through")
                if self.plan_action in {"accept", "execute"} and self.plan_id is None:
                    _invalid(f"Plan {self.plan_action} recovery action requires plan_id")
        elif self.plan_id is not None:
            _invalid("plan_id is only valid for workspace_verify plan accept or execute")
        if self.kind is RecoveryActionKind.OPERATION:
            if self.operation_id is None:
                _invalid("operation recovery action requires operation_id")
            if self.action not in {"get", "cancel"}:
                _invalid("operation recovery action requires action of get or cancel")
        else:
            if self.operation_id is not None:
                _invalid("operation_id is only valid for operation recovery actions")
        if self.kind is RecoveryActionKind.WORKSPACE_REFRESH:
            if self.action is None:
                _invalid("workspace_refresh recovery action requires action")
            if self.action not in {"preview", "apply"}:
                _invalid("Recovery action action is invalid for workspace_refresh")
            if self.expected_head_sha is None or self.expected_workspace_fingerprint is None:
                _invalid(
                    "workspace_refresh recovery action requires expected_head_sha and "
                    "expected_workspace_fingerprint"
                )
        elif self.kind is not RecoveryActionKind.OPERATION and self.action is not None:
            _invalid("action is only valid for operation or workspace_refresh recovery actions")
        if self.kind is RecoveryActionKind.WORKSPACE_MUTATE:
            if not self.relative_paths:
                _invalid("Restore recovery action requires relative_paths")
            if self.expected_head_sha is None or self.expected_workspace_fingerprint is None:
                _invalid(
                    "Restore recovery action requires expected_head_sha and "
                    "expected_workspace_fingerprint"
                )
        if self.kind not in {
            RecoveryActionKind.WORKSPACE_REFRESH,
            RecoveryActionKind.WORKSPACE_MUTATE,
        } and (
            self.expected_head_sha is not None or self.expected_workspace_fingerprint is not None
        ):
            _invalid(
                "expected_head_sha and expected_workspace_fingerprint are only valid for "
                "workspace_refresh or workspace_mutate recovery actions"
            )

    def _exact_arguments(self) -> dict[str, object]:
        if self.kind is RecoveryActionKind.OPERATION:
            return {"action": self.action, "operation_id": self.operation_id}
        if self.kind is RecoveryActionKind.WORKSPACE_STATUS:
            return {"workspace_id": self.workspace_id}
        if self.kind is RecoveryActionKind.CONFIG_INSPECT:
            return {}
        if self.kind is RecoveryActionKind.WORKSPACE_VERIFY:
            arguments: dict[str, object] = {
                "workspace_id": self.workspace_id,
                "mode": self.mode,
            }
            if self.diagnostic_id is not None:
                arguments["diagnostic_id"] = self.diagnostic_id
            if self.profile_name is not None:
                arguments["profile_name"] = self.profile_name
            if self.plan_action is not None:
                arguments["plan_action"] = self.plan_action
            if self.plan_id is not None:
                arguments["plan_id"] = self.plan_id
            if self.plan_through is not None:
                arguments["plan_through"] = self.plan_through
            return arguments
        if self.kind is RecoveryActionKind.WORKSPACE_REFRESH:
            return {
                "workspace_id": self.workspace_id,
                "action": self.action,
                "expected_head_sha": self.expected_head_sha,
                "expected_fingerprint": self.expected_workspace_fingerprint,
            }
        if self.kind is RecoveryActionKind.WORKSPACE_MUTATE:
            return {
                "workspace_id": self.workspace_id,
                "operations": [{"op": "restore", "paths": list(self.relative_paths)}],
                "expected_head_sha": self.expected_head_sha,
                "expected_workspace_fingerprint": self.expected_workspace_fingerprint,
            }
        raise AssertionError(f"Unhandled recovery action kind: {self.kind.value}")

    def payload(self) -> dict[str, object]:
        """Return a directly callable public recovery action."""

        return {
            "kind": self.kind.value,
            "precondition": self.precondition,
            "arguments": self._exact_arguments(),
        }

    def legacy_payload(self) -> dict[str, object]:
        """Stable schema-v1 identity representation used by persisted evidence hashes."""

        return {
            "action": self.action,
            "diagnostic_id": self.diagnostic_id,
            "expected_head_sha": self.expected_head_sha,
            "expected_workspace_fingerprint": self.expected_workspace_fingerprint,
            "kind": self.kind.value,
            "mode": self.mode,
            "operation_id": self.operation_id,
            "plan_action": self.plan_action,
            "plan_id": self.plan_id,
            "plan_through": self.plan_through,
            "precondition": self.precondition,
            "profile_name": self.profile_name,
            "relative_paths": list(self.relative_paths),
            "workspace_id": self.workspace_id,
        }


@dataclass(frozen=True, slots=True)
class FailureHistorySignal:
    binding_hash: str
    outcome: str

    def __post_init__(self) -> None:
        if _SHA64.fullmatch(self.binding_hash) is None:
            _invalid("Failure history binding hash is invalid")
        if self.outcome not in {"succeeded", "failed"}:
            _invalid("Failure history outcome is invalid")


@dataclass(frozen=True, slots=True)
class FailureObservation:
    operation_id: str
    plan_id: str
    plan_hash: str
    stage_id: str
    stage_kind: str
    target: str
    workspace_id: str
    pre_identity: WorkspaceIdentity
    post_identity: WorkspaceIdentity
    environment_identity: str | None
    error_code: str | None
    message: str
    details: dict[str, object]
    failure_domain: str | None
    changed_paths: tuple[str, ...]
    history: tuple[FailureHistorySignal, ...]
    compatibility_binding: str | None = None

    def __post_init__(self) -> None:
        if _SAFE_ID.fullmatch(self.workspace_id) is None:
            _invalid("Failure observation workspace_id is invalid")
        if _OPERATION_ID.fullmatch(self.operation_id) is None:
            _invalid("Failure observation operation_id is invalid")
        if _PLAN_ID.fullmatch(self.plan_id) is None or _SHA64.fullmatch(self.plan_hash) is None:
            _invalid("Failure observation plan identity is invalid")
        for field, value in (
            ("stage_id", self.stage_id),
            ("stage_kind", self.stage_kind),
            ("target", self.target),
        ):
            if _SAFE_ID.fullmatch(value) is None:
                _invalid(f"Failure observation {field} is invalid")
        if (
            self.environment_identity is not None
            and _SHA64.fullmatch(self.environment_identity) is None
        ):
            _invalid("Failure observation environment identity is invalid")
        if self.error_code is not None and _SAFE_ID.fullmatch(self.error_code) is None:
            _invalid("Failure observation error code is invalid")
        _safe_text(self.message, "failure observation message", 1_000_000)
        if not isinstance(self.details, dict) or len(self.details) > 200:
            _invalid("Failure observation details must be a bounded mapping")
        if self.failure_domain is not None and _SAFE_ID.fullmatch(self.failure_domain) is None:
            _invalid("Failure observation failure_domain is invalid")
        object.__setattr__(self, "changed_paths", _safe_paths(self.changed_paths, "changed_paths"))
        if not isinstance(self.history, tuple) or len(self.history) > 100:
            _invalid("Failure observation history must be a bounded tuple")
        if (
            self.compatibility_binding is not None
            and _SHA64.fullmatch(self.compatibility_binding) is None
        ):
            _invalid("Failure observation compatibility binding is invalid")


@dataclass(frozen=True, slots=True)
class FailureClassification:
    failure_class: FailureClass
    stable_error_code: str
    retryable: bool
    confidence: int
    reproducibility: FailureReproducibility
    first_diagnostic: str
    excerpt: str
    excerpt_sha256: str
    source_digest: str
    uncertainty: tuple[str, ...]
    safe_actions: tuple[RecoveryAction, ...]


@dataclass(frozen=True, slots=True)
class FailureScope:
    paths: tuple[str, ...]
    tests: tuple[str, ...]
    symbols: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "paths", _safe_paths(self.paths, "failure scope paths"))
        object.__setattr__(self, "tests", _safe_strings(self.tests, "failure scope tests"))
        object.__setattr__(self, "symbols", _safe_strings(self.symbols, "failure scope symbols"))

    def payload(self) -> dict[str, object]:
        return {
            "paths": list(self.paths),
            "symbols": list(self.symbols),
            "tests": list(self.tests),
        }


@dataclass(frozen=True, slots=True)
class FailureEvidence:
    failure_id: str
    operation_id: str
    plan_id: str
    plan_hash: str
    stage_id: str
    receipt_id: str | None
    pre_identity: WorkspaceIdentity
    post_identity: WorkspaceIdentity
    environment_identity: str | None
    compatibility_binding: str
    failure_class: FailureClass
    stable_error_code: str
    first_diagnostic: str
    excerpt: str
    excerpt_sha256: str
    excerpt_reference: str
    affected_scope: FailureScope
    reproducibility: FailureReproducibility
    files_changed: bool
    retryable: bool
    confidence: int
    uncertainty: tuple[str, ...]
    safe_actions: tuple[RecoveryAction, ...]
    source_digest: str
    created_at: str
    schema_version: int = FAILURE_EVIDENCE_SCHEMA_VERSION


def _invalid(message: str) -> NoReturn:
    raise RepoForgeError(
        message,
        code=ErrorCode.EVIDENCE_INVALID,
        safe_next_action="Rebuild failure evidence from bounded structured execution output.",
    )


def _safe_text(value: str, field: str, limit: int) -> str:
    if not isinstance(value, str):
        _invalid(f"{field} must be text")
    normalized = value.strip()
    if not normalized or len(normalized) > limit:
        _invalid(f"{field} must contain between 1 and {limit} characters")
    if any(ord(character) < 32 and character not in "\n\t\r" for character in normalized):
        _invalid(f"{field} contains unsupported control characters")
    return normalized


def _safe_paths(values: tuple[str, ...], field: str) -> tuple[str, ...]:
    if not isinstance(values, tuple) or len(values) > _MAX_SCOPE_ITEMS:
        _invalid(f"{field} must be a bounded tuple")
    normalized: set[str] = set()
    for value in values:
        item = _safe_text(value, field, 512).replace("\\", "/")
        if item.startswith("/") or any(part in {"", ".", ".."} for part in item.split("/")):
            _invalid(f"{field} contains an unsafe path")
        normalized.add(item)
    return tuple(sorted(normalized))


def _safe_strings(values: tuple[str, ...], field: str) -> tuple[str, ...]:
    if not isinstance(values, tuple) or len(values) > _MAX_SCOPE_ITEMS:
        _invalid(f"{field} must be a bounded tuple")
    return tuple(sorted({_safe_text(item, field, 512) for item in values}))


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _identity_payload(identity: WorkspaceIdentity) -> dict[str, str]:
    return identity.payload()


def failure_compatibility_binding(observation: FailureObservation) -> str:
    return observation.compatibility_binding or _digest(
        {
            "environment_identity": observation.environment_identity,
            "plan_hash": observation.plan_hash,
            "pre_identity": _identity_payload(observation.pre_identity),
            "stage_id": observation.stage_id,
            "target": observation.target,
        }
    )


def _sanitize_output(message: str) -> tuple[str, str, str, bool]:
    encoded_size = len(message.encode("utf-8", errors="replace"))
    result = evaluate_egress(
        EgressRequest(
            message,
            EgressContentClass.DIAGNOSTIC,
            EgressDestination.MODEL,
            policy=EgressPolicy(
                max_input_bytes=max(1, min(max(encoded_size, 1), 1_000_000)),
                max_output_chars=_MAX_EXCERPT_CHARS,
                max_output_lines=80,
                withhold_private_keys=False,
            ),
        )
    )
    excerpt = result.content or f"<{result.decision.value}:{result.reason}>"
    first = next(
        (line.strip() for line in excerpt.splitlines() if line.strip()),
        "Failure output unavailable",
    )
    return excerpt, first[:_MAX_DIAGNOSTIC_CHARS], result.source_digest, result.truncated


_STRUCTURED_CODE_MAP: dict[str, FailureClass] = {
    ErrorCode.DIAGNOSTIC_TOOL_MISSING.value: FailureClass.TOOL_MISSING,
    ErrorCode.CONFIG_INVALID.value: FailureClass.CONFIGURATION_INVALID,
    ErrorCode.CONFIG_STALE.value: FailureClass.CONFIGURATION_INVALID,
    ErrorCode.CONFIG_CHANGED.value: FailureClass.CONFIGURATION_INVALID,
    ErrorCode.COMMAND_TIMEOUT.value: FailureClass.TIMEOUT,
    ErrorCode.DIAGNOSTIC_TIMEOUT.value: FailureClass.TIMEOUT,
    ErrorCode.PR_CHECK_WATCH_TIMEOUT.value: FailureClass.TIMEOUT,
    ErrorCode.SECURITY_POLICY_VIOLATION.value: FailureClass.POLICY_FAILURE,
    ErrorCode.DIAGNOSTIC_STALE_WORKSPACE.value: FailureClass.STALE_WORKSPACE,
    ErrorCode.STALE_ASSESSMENT_SNAPSHOT.value: FailureClass.STALE_WORKSPACE,
    ErrorCode.DIAGNOSTIC_UNEXPECTED_MUTATION.value: FailureClass.UNEXPECTED_MUTATION,
    ErrorCode.CODE_INTELLIGENCE_UNAVAILABLE.value: FailureClass.PROVIDER_FAILURE,
    ErrorCode.CODE_INTELLIGENCE_PARTIAL.value: FailureClass.PROVIDER_FAILURE,
}

_DOMAIN_MAP: dict[str, FailureClass] = {
    "hygiene": FailureClass.LINT_FAILURE,
    "static_analysis": FailureClass.LINT_FAILURE,
    "typecheck": FailureClass.TYPE_FAILURE,
    "business_tests": FailureClass.TEST_FAILURE,
    "build": FailureClass.BUILD_FAILURE,
}

_TEXT_RULES: tuple[tuple[tuple[str, ...], FailureClass], ...] = (
    (
        ("executable not found", "command not found", "no such executable"),
        FailureClass.TOOL_MISSING,
    ),
    (
        ("modulenotfounderror", "no module named", "dependency missing"),
        FailureClass.DEPENDENCY_MISSING,
    ),
    (
        ("environment mismatch", "python version mismatch", "platform mismatch"),
        FailureClass.ENVIRONMENT_MISMATCH,
    ),
    (
        ("invalid configuration", "config is invalid", "toml parse"),
        FailureClass.CONFIGURATION_INVALID,
    ),
    (("timed out", "timeout"), FailureClass.TIMEOUT),
    (("cancelled", "canceled"), FailureClass.CANCELLED),
    (("ruff", "lint failed", "flake8"), FailureClass.LINT_FAILURE),
    (("mypy", "typecheck", "type error"), FailureClass.TYPE_FAILURE),
    (("pytest", "test failed", "assertionerror"), FailureClass.TEST_FAILURE),
    (("build failed", "wheel build", "sdist"), FailureClass.BUILD_FAILURE),
    (
        (
            "connection refused",
            "dns",
            "network is unreachable",
            "http 429",
            "http 502",
            "http 503",
            "http 504",
        ),
        FailureClass.NETWORK_FAILURE,
    ),
    (
        ("permission denied", "operation not permitted", "access denied"),
        FailureClass.PERMISSION_FAILURE,
    ),
    (("policy violation", "denied by policy", "protected path"), FailureClass.POLICY_FAILURE),
    (("stale workspace", "workspace changed since"), FailureClass.STALE_WORKSPACE),
    (("stale plan", "plan no longer matches", "execution plan no longer"), FailureClass.STALE_PLAN),
    (("unexpected mutation", "changed paths outside"), FailureClass.UNEXPECTED_MUTATION),
    (
        ("provider unavailable", "provider failure", "provider returned"),
        FailureClass.PROVIDER_FAILURE,
    ),
)


def _structured_class(observation: FailureObservation) -> tuple[FailureClass | None, int]:
    raw = observation.details.get("failure_class")
    if isinstance(raw, str):
        try:
            return FailureClass(raw), 98
        except ValueError:
            pass
    if observation.details.get("cancelled") is True:
        return FailureClass.CANCELLED, 98
    if observation.error_code == ErrorCode.STATE_STALE.value and isinstance(
        observation.details.get("plan_id"), str
    ):
        return FailureClass.STALE_PLAN, 95
    if observation.error_code in _STRUCTURED_CODE_MAP:
        return _STRUCTURED_CODE_MAP[observation.error_code], 95
    if observation.failure_domain in _DOMAIN_MAP:
        return _DOMAIN_MAP[observation.failure_domain], 90
    return None, 0


def _text_class(message: str) -> tuple[FailureClass, int]:
    lowered = message.casefold()
    for markers, failure_class in _TEXT_RULES:
        if any(marker in lowered for marker in markers):
            return failure_class, 70
    return FailureClass.UNKNOWN, 20


def _flaky(observation: FailureObservation, base: FailureClass) -> bool:
    if base not in {
        FailureClass.LINT_FAILURE,
        FailureClass.TYPE_FAILURE,
        FailureClass.TEST_FAILURE,
        FailureClass.BUILD_FAILURE,
    }:
        return False
    binding = failure_compatibility_binding(observation)
    outcomes = {signal.outcome for signal in observation.history if signal.binding_hash == binding}
    return outcomes == {"succeeded", "failed"}


def _reproducibility(
    observation: FailureObservation, failure_class: FailureClass
) -> FailureReproducibility:
    if failure_class is FailureClass.FLAKY_SUSPECTED:
        return FailureReproducibility.INTERMITTENT
    if (
        observation.pre_identity != observation.post_identity
        or observation.environment_identity is None
        or failure_class
        in {
            FailureClass.NETWORK_FAILURE,
            FailureClass.PROVIDER_FAILURE,
            FailureClass.CANCELLED,
            FailureClass.UNKNOWN,
        }
    ):
        return FailureReproducibility.UNKNOWN
    return FailureReproducibility.REPRODUCIBLE


def _action(kind: RecoveryActionKind, precondition: str, **kwargs: object) -> RecoveryAction:
    return RecoveryAction(kind=kind, precondition=precondition, **kwargs)  # type: ignore[arg-type]


def _actions(
    observation: FailureObservation, failure_class: FailureClass
) -> tuple[RecoveryAction, ...]:
    status = _action(
        RecoveryActionKind.WORKSPACE_STATUS,
        "The workspace still exists and the caller needs a fresh HEAD and fingerprint.",
        workspace_id=observation.workspace_id,
    )
    operation = _action(
        RecoveryActionKind.OPERATION,
        "The durable operation ID remains available.",
        operation_id=observation.operation_id,
        action="get",
    )
    profile = _action(
        RecoveryActionKind.WORKSPACE_VERIFY,
        "The named reviewed profile remains configured for this repository.",
        workspace_id=observation.workspace_id,
        mode="profile",
        profile_name=observation.target if observation.stage_kind == "profile" else "quick",
    )
    plan = _action(
        RecoveryActionKind.WORKSPACE_VERIFY,
        "Workspace status, configuration, policy, and assessment evidence are current.",
        workspace_id=observation.workspace_id,
        mode="plan",
        plan_action="create",
    )
    config = _action(
        RecoveryActionKind.CONFIG_INSPECT,
        "The operator is reviewing active configuration without applying a mutation.",
    )
    if failure_class in {FailureClass.TOOL_MISSING, FailureClass.DEPENDENCY_MISSING}:
        setup = _action(
            RecoveryActionKind.WORKSPACE_VERIFY,
            "The reviewed setup profile is configured and network policy permits dependency preparation.",
            workspace_id=observation.workspace_id,
            mode="profile",
            profile_name="setup",
        )
        return (setup, profile, status)
    if failure_class is FailureClass.ENVIRONMENT_MISMATCH:
        return (config, status, profile)
    if failure_class is FailureClass.CONFIGURATION_INVALID:
        return (config, status, plan)
    if failure_class in {FailureClass.TIMEOUT, FailureClass.CANCELLED}:
        # A fresh plan, not a re-execute of the plan bound to this failed
        # attempt: the stage that just timed out/was cancelled belongs to
        # `observation.plan_id`, and re-executing that exact plan_id would
        # just retry the same accepted stage sequence against workspace
        # bindings that are, at minimum, unverified since the failure.
        return (operation, status, plan)
    if failure_class in {
        FailureClass.LINT_FAILURE,
        FailureClass.TYPE_FAILURE,
        FailureClass.TEST_FAILURE,
        FailureClass.BUILD_FAILURE,
        FailureClass.FLAKY_SUSPECTED,
    }:
        return (profile, status, plan)
    if failure_class in {FailureClass.NETWORK_FAILURE, FailureClass.PROVIDER_FAILURE}:
        return (operation, status, config)
    if failure_class in {FailureClass.PERMISSION_FAILURE, FailureClass.POLICY_FAILURE}:
        return (config, status)
    if failure_class is FailureClass.STALE_WORKSPACE:
        refresh = _action(
            RecoveryActionKind.WORKSPACE_REFRESH,
            "The workspace is clean enough to review a new exact remote-base preview.",
            workspace_id=observation.workspace_id,
            action="preview",
            expected_head_sha=observation.post_identity.head_sha,
            expected_workspace_fingerprint=observation.post_identity.workspace_fingerprint,
        )
        return (status, refresh, plan)
    if failure_class is FailureClass.STALE_PLAN:
        # `observation.plan_id` is the plan this classification just found
        # stale -- recommending an execute of that same plan_id would just
        # reproduce the staleness. The only safe recovery is a fresh plan.
        return (status, plan)
    if failure_class is FailureClass.UNEXPECTED_MUTATION:
        if observation.changed_paths:
            restore = _action(
                RecoveryActionKind.WORKSPACE_MUTATE,
                "The listed paths were reviewed and the operator intends to discard those exact changes.",
                workspace_id=observation.workspace_id,
                relative_paths=observation.changed_paths,
                expected_head_sha=observation.post_identity.head_sha,
                expected_workspace_fingerprint=observation.post_identity.workspace_fingerprint,
            )
            return (status, restore, plan)
        return (status, plan)
    return (operation, status)


def classify_failure(observation: FailureObservation) -> FailureClassification:
    excerpt, first_diagnostic, source_digest, truncated = _sanitize_output(observation.message)
    failure_class, confidence = _structured_class(observation)
    if failure_class is None:
        failure_class, confidence = _text_class(observation.message)
    if _flaky(observation, failure_class):
        failure_class = FailureClass.FLAKY_SUSPECTED
        confidence = max(confidence, 85)
    reproducibility = _reproducibility(observation, failure_class)
    uncertainty: list[str] = []
    if confidence < 80:
        uncertainty.append(
            "Classification relied on bounded text heuristics rather than structured output."
        )
    if truncated:
        uncertainty.append("Diagnostic output was truncated to the reviewed evidence bound.")
    if reproducibility is FailureReproducibility.UNKNOWN:
        uncertainty.append(
            "Reproducibility is unknown because input/environment identity or failure stability is incomplete."
        )
    if failure_class is FailureClass.UNKNOWN:
        uncertainty.append("No reviewed structured or textual classifier matched this failure.")
    stable_code = observation.error_code or f"FAILURE_{failure_class.value.upper()}"
    return FailureClassification(
        failure_class=failure_class,
        stable_error_code=stable_code,
        retryable=failure_class
        in {
            FailureClass.TIMEOUT,
            FailureClass.NETWORK_FAILURE,
            FailureClass.PROVIDER_FAILURE,
            FailureClass.STALE_WORKSPACE,
            FailureClass.STALE_PLAN,
            FailureClass.FLAKY_SUSPECTED,
        },
        confidence=confidence,
        reproducibility=reproducibility,
        first_diagnostic=first_diagnostic,
        excerpt=excerpt,
        excerpt_sha256=hashlib.sha256(excerpt.encode("utf-8")).hexdigest(),
        source_digest=source_digest,
        uncertainty=tuple(uncertainty),
        safe_actions=_actions(observation, failure_class),
    )


def _detail_strings(details: dict[str, object], key: str) -> tuple[str, ...]:
    value = details.get(key)
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(str(item) for item in value[:_MAX_SCOPE_ITEMS] if isinstance(item, str))


def _evidence_identity_payload(
    evidence: FailureEvidence, *, public_actions: bool = False
) -> dict[str, object]:
    return {
        "affected_scope": evidence.affected_scope.payload(),
        "compatibility_binding": evidence.compatibility_binding,
        "confidence": evidence.confidence,
        "created_at": evidence.created_at,
        "environment_identity": evidence.environment_identity,
        "excerpt_reference": evidence.excerpt_reference,
        "excerpt_sha256": evidence.excerpt_sha256,
        "failure_class": evidence.failure_class.value,
        "files_changed": evidence.files_changed,
        "first_diagnostic": evidence.first_diagnostic,
        "operation_id": evidence.operation_id,
        "plan_hash": evidence.plan_hash,
        "plan_id": evidence.plan_id,
        "post_identity": _identity_payload(evidence.post_identity),
        "pre_identity": _identity_payload(evidence.pre_identity),
        "reproducibility": evidence.reproducibility.value,
        "retryable": evidence.retryable,
        "safe_actions": [
            action.payload() if public_actions else action.legacy_payload()
            for action in evidence.safe_actions
        ],
        "schema_version": evidence.schema_version,
        "source_digest": evidence.source_digest,
        "stable_error_code": evidence.stable_error_code,
        "stage_id": evidence.stage_id,
        "uncertainty": list(evidence.uncertainty),
    }


def build_failure_evidence(
    observation: FailureObservation,
    *,
    created_at: str,
    receipt_id: str | None = None,
) -> FailureEvidence:
    if receipt_id is not None and _RECEIPT_ID.fullmatch(receipt_id) is None:
        _invalid("Failure evidence receipt_id is invalid")
    classification = classify_failure(observation)
    scope = FailureScope(
        paths=tuple(
            sorted(
                set(observation.changed_paths) | set(_detail_strings(observation.details, "paths"))
            )
        ),
        tests=_detail_strings(observation.details, "tests"),
        symbols=_detail_strings(observation.details, "symbols"),
    )
    provisional = FailureEvidence(
        failure_id="failure-" + "0" * 24,
        operation_id=observation.operation_id,
        plan_id=observation.plan_id,
        plan_hash=observation.plan_hash,
        stage_id=observation.stage_id,
        receipt_id=receipt_id,
        pre_identity=observation.pre_identity,
        post_identity=observation.post_identity,
        environment_identity=observation.environment_identity,
        compatibility_binding=failure_compatibility_binding(observation),
        failure_class=classification.failure_class,
        stable_error_code=classification.stable_error_code,
        first_diagnostic=classification.first_diagnostic,
        excerpt=classification.excerpt,
        excerpt_sha256=classification.excerpt_sha256,
        excerpt_reference=f"failure-output:{classification.source_digest[:24]}",
        affected_scope=scope,
        reproducibility=classification.reproducibility,
        files_changed=bool(observation.changed_paths)
        or observation.pre_identity.workspace_fingerprint
        != observation.post_identity.workspace_fingerprint,
        retryable=classification.retryable,
        confidence=classification.confidence,
        uncertainty=classification.uncertainty,
        safe_actions=classification.safe_actions,
        source_digest=classification.source_digest,
        created_at=created_at,
    )
    digest = _digest(_evidence_identity_payload(provisional))
    return validate_failure_evidence(replace(provisional, failure_id=f"failure-{digest[:24]}"))


def validate_failure_evidence(evidence: FailureEvidence) -> FailureEvidence:
    if evidence.schema_version != FAILURE_EVIDENCE_SCHEMA_VERSION:
        raise RepoForgeError(
            "Failure evidence schema version is unsupported",
            code=ErrorCode.EVIDENCE_SCHEMA_UNSUPPORTED,
        )
    if _FAILURE_ID.fullmatch(evidence.failure_id) is None:
        _invalid("Failure evidence ID is invalid")
    if _OPERATION_ID.fullmatch(evidence.operation_id) is None:
        _invalid("Failure evidence operation ID is invalid")
    if _PLAN_ID.fullmatch(evidence.plan_id) is None or _SHA64.fullmatch(evidence.plan_hash) is None:
        _invalid("Failure evidence plan identity is invalid")
    if _SAFE_ID.fullmatch(evidence.stage_id) is None:
        _invalid("Failure evidence stage ID is invalid")
    if evidence.receipt_id is not None and _RECEIPT_ID.fullmatch(evidence.receipt_id) is None:
        _invalid("Failure evidence receipt ID is invalid")
    if (
        evidence.environment_identity is not None
        and _SHA64.fullmatch(evidence.environment_identity) is None
    ):
        _invalid("Failure evidence environment identity is invalid")
    if _SHA64.fullmatch(evidence.compatibility_binding) is None:
        _invalid("Failure evidence compatibility binding is invalid")
    if _SAFE_ID.fullmatch(evidence.stable_error_code) is None:
        _invalid("Failure evidence stable error code is invalid")
    _safe_text(evidence.first_diagnostic, "first diagnostic", _MAX_DIAGNOSTIC_CHARS)
    _safe_text(evidence.excerpt, "failure excerpt", _MAX_EXCERPT_CHARS)
    if hashlib.sha256(evidence.excerpt.encode("utf-8")).hexdigest() != evidence.excerpt_sha256:
        _invalid("Failure evidence excerpt digest mismatch")
    if _SHA64.fullmatch(evidence.source_digest) is None:
        _invalid("Failure evidence source digest is invalid")
    if (
        not isinstance(evidence.confidence, int)
        or isinstance(evidence.confidence, bool)
        or not 0 <= evidence.confidence <= 100
    ):
        _invalid("Failure evidence confidence is invalid")
    if not isinstance(evidence.retryable, bool) or not isinstance(evidence.files_changed, bool):
        _invalid("Failure evidence boolean fields are invalid")
    _safe_strings(evidence.uncertainty, "failure uncertainty")
    if not evidence.safe_actions or len(evidence.safe_actions) > 20:
        _invalid("Failure evidence safe actions are invalid")
    expected = "failure-" + _digest(_evidence_identity_payload(evidence))[:24]
    if evidence.failure_id != expected:
        _invalid("Failure evidence ID does not match normalized identity")
    return evidence


def failure_evidence_payload(evidence: FailureEvidence) -> dict[str, object]:
    validate_failure_evidence(evidence)
    return {
        "failure_id": evidence.failure_id,
        "receipt_id": evidence.receipt_id,
        "excerpt": evidence.excerpt,
        **_evidence_identity_payload(evidence, public_actions=True),
    }


def failure_evidence_from_payload(payload: dict[str, Any]) -> FailureEvidence:
    def identity(field: str) -> WorkspaceIdentity:
        value = payload.get(field)
        if not isinstance(value, dict):
            _invalid(f"Failure evidence {field} is missing")
        return WorkspaceIdentity(
            head_sha=str(value.get("head_sha", "")),
            workspace_fingerprint=str(value.get("workspace_fingerprint", "")),
            config_generation=str(value.get("config_generation", "")),
            policy_hash=str(value.get("policy_hash", "")),
        )

    raw_scope = payload.get("affected_scope")
    raw_actions = payload.get("safe_actions")
    raw_uncertainty = payload.get("uncertainty")
    if (
        not isinstance(raw_scope, dict)
        or not isinstance(raw_actions, list)
        or not isinstance(raw_uncertainty, list)
    ):
        _invalid("Failure evidence payload is incomplete")
    actions: list[RecoveryAction] = []
    for raw in raw_actions:
        if not isinstance(raw, dict):
            _invalid("Failure recovery action payload is invalid")
        kind = RecoveryActionKind(str(raw.get("kind", "")))
        raw_arguments = raw.get("arguments")
        arguments = raw_arguments if isinstance(raw_arguments, dict) else raw
        raw_operations = arguments.get("operations", [])
        raw_paths = arguments.get("relative_paths", [])
        if kind is RecoveryActionKind.WORKSPACE_MUTATE and isinstance(raw_operations, list):
            restore = next(
                (
                    item
                    for item in raw_operations
                    if isinstance(item, dict) and item.get("op") == "restore"
                ),
                None,
            )
            if isinstance(restore, dict):
                raw_paths = restore.get("paths", [])
        if not isinstance(raw_paths, list):
            _invalid("Failure recovery action paths are invalid")
        actions.append(
            RecoveryAction(
                kind=kind,
                precondition=str(raw.get("precondition", "")),
                workspace_id=(
                    str(arguments["workspace_id"])
                    if arguments.get("workspace_id") is not None
                    else None
                ),
                mode=(str(arguments["mode"]) if arguments.get("mode") is not None else None),
                plan_action=(
                    str(arguments["plan_action"])
                    if arguments.get("plan_action") is not None
                    else None
                ),
                diagnostic_id=(
                    str(arguments["diagnostic_id"])
                    if arguments.get("diagnostic_id") is not None
                    else None
                ),
                profile_name=(
                    str(arguments["profile_name"])
                    if arguments.get("profile_name") is not None
                    else None
                ),
                plan_through=(
                    str(arguments["plan_through"])
                    if arguments.get("plan_through") is not None
                    else None
                ),
                relative_paths=tuple(str(item) for item in raw_paths),
                operation_id=(
                    str(arguments["operation_id"])
                    if arguments.get("operation_id") is not None
                    else None
                ),
                plan_id=(
                    str(arguments["plan_id"]) if arguments.get("plan_id") is not None else None
                ),
                action=(str(arguments["action"]) if arguments.get("action") is not None else None),
                expected_head_sha=(
                    str(arguments["expected_head_sha"])
                    if arguments.get("expected_head_sha") is not None
                    else None
                ),
                expected_workspace_fingerprint=(
                    str(
                        arguments.get(
                            "expected_workspace_fingerprint",
                            arguments.get("expected_fingerprint"),
                        )
                    )
                    if arguments.get("expected_workspace_fingerprint") is not None
                    or arguments.get("expected_fingerprint") is not None
                    else None
                ),
            )
        )
    evidence = FailureEvidence(
        failure_id=str(payload.get("failure_id", "")),
        operation_id=str(payload.get("operation_id", "")),
        plan_id=str(payload.get("plan_id", "")),
        plan_hash=str(payload.get("plan_hash", "")),
        stage_id=str(payload.get("stage_id", "")),
        receipt_id=(str(payload["receipt_id"]) if payload.get("receipt_id") is not None else None),
        pre_identity=identity("pre_identity"),
        post_identity=identity("post_identity"),
        environment_identity=(
            str(payload["environment_identity"])
            if payload.get("environment_identity") is not None
            else None
        ),
        compatibility_binding=str(payload.get("compatibility_binding", "")),
        failure_class=FailureClass(str(payload.get("failure_class", ""))),
        stable_error_code=str(payload.get("stable_error_code", "")),
        first_diagnostic=str(payload.get("first_diagnostic", "")),
        excerpt=str(payload.get("excerpt", "")),
        excerpt_sha256=str(payload.get("excerpt_sha256", "")),
        excerpt_reference=str(payload.get("excerpt_reference", "")),
        affected_scope=FailureScope(
            paths=tuple(str(item) for item in raw_scope.get("paths", [])),
            tests=tuple(str(item) for item in raw_scope.get("tests", [])),
            symbols=tuple(str(item) for item in raw_scope.get("symbols", [])),
        ),
        reproducibility=FailureReproducibility(str(payload.get("reproducibility", ""))),
        files_changed=bool(payload.get("files_changed", False)),
        retryable=bool(payload.get("retryable", False)),
        confidence=int(payload.get("confidence", -1)),
        uncertainty=tuple(str(item) for item in raw_uncertainty),
        safe_actions=tuple(actions),
        source_digest=str(payload.get("source_digest", "")),
        created_at=str(payload.get("created_at", "")),
        schema_version=int(payload.get("schema_version", 0)),
    )
    return validate_failure_evidence(evidence)
