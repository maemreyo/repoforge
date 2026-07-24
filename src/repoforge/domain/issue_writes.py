"""Governed policy for explicit GitHub issue mutations."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from string import Formatter
from typing import Any


class IssueWritePolicyError(ValueError):
    """Raised when issue-write policy is malformed or unsafe."""


class IssueWriteOperation(str, Enum):
    COMMENT = "comment"
    CLOSE = "close"
    REOPEN = "reopen"
    LINK = "link"
    CREATE = "create"
    UPDATE = "update"


class IssueLinkType(str, Enum):
    SUB_ISSUE = "sub_issue"
    BLOCKED_BY = "blocked_by"
    SUPERSEDE = "supersede"


_OPERATION_ORDER = tuple(operation.value for operation in IssueWriteOperation)
_ALLOWED_TEMPLATE_FIELDS = frozenset({"body", "evidence_ref"})
_DEFAULT_CREATE_TEMPLATE = "## Objective\n{body}\n\n## Evidence\n{evidence_ref}"


def _bounded_text(value: object, context: str, *, limit: int) -> str:
    if not isinstance(value, str):
        raise IssueWritePolicyError(f"{context} must be a string")
    normalized = value.strip()
    if not normalized or len(normalized) > limit:
        raise IssueWritePolicyError(f"{context} must contain between 1 and {limit} characters")
    if any(ord(character) < 32 and character not in "\t\n" for character in normalized):
        raise IssueWritePolicyError(f"{context} contains control characters")
    return normalized


def _bounded_int(value: object, context: str, *, default: int, minimum: int, maximum: int) -> int:
    selected = default if value is None else value
    if (
        not isinstance(selected, int)
        or isinstance(selected, bool)
        or not minimum <= selected <= maximum
    ):
        raise IssueWritePolicyError(f"{context} must be between {minimum} and {maximum}")
    return selected


def _operations(value: object, context: str, *, default: tuple[str, ...]) -> tuple[str, ...]:
    if value is None:
        raw = default
    elif isinstance(value, list) and all(isinstance(item, str) for item in value):
        raw = tuple(value)
    else:
        raise IssueWritePolicyError(f"{context} must be an array of operation names")
    unknown = sorted(set(raw) - set(_OPERATION_ORDER))
    if unknown:
        raise IssueWritePolicyError(f"{context} contains unsupported operations: {unknown}")
    if len(raw) != len(set(raw)):
        raise IssueWritePolicyError(f"{context} contains duplicate operations")
    return tuple(operation for operation in _OPERATION_ORDER if operation in raw)


def _template(value: object, context: str) -> str:
    selected = (
        _DEFAULT_CREATE_TEMPLATE if value is None else _bounded_text(value, context, limit=10_000)
    )
    fields: set[str] = set()
    try:
        for _, field_name, _, _ in Formatter().parse(selected):
            if field_name is not None:
                fields.add(field_name)
    except ValueError as exc:
        raise IssueWritePolicyError(f"{context} is not a valid format template") from exc
    if fields != _ALLOWED_TEMPLATE_FIELDS:
        raise IssueWritePolicyError(
            f"{context} must contain exactly {{body}} and {{evidence_ref}} placeholders"
        )
    return selected


@dataclass(frozen=True, slots=True)
class IssueWritePolicy:
    """Per-repository allowlist and bounded external-mutation budget."""

    enabled_ops: tuple[str, ...] = (IssueWriteOperation.COMMENT.value,)
    approval_required_ops: tuple[str, ...] = ()
    operation_semantics_version: int = 1
    max_writes_per_call: int = 2
    max_writes_per_window: int = 20
    window_seconds: int = 3_600
    create_title_prefix: str = "[TASK]"
    create_body_template: str = _DEFAULT_CREATE_TEMPLATE

    def __post_init__(self) -> None:
        enabled = _operations(
            list(self.enabled_ops), "issue_writes.enabled_ops", default=("comment",)
        )
        approval = _operations(
            list(self.approval_required_ops),
            "issue_writes.approval_required_ops",
            default=(),
        )
        if not set(approval).issubset(enabled):
            raise IssueWritePolicyError(
                "issue_writes.approval_required_ops must be a subset of enabled_ops"
            )
        object.__setattr__(self, "enabled_ops", enabled)
        object.__setattr__(self, "approval_required_ops", approval)
        object.__setattr__(
            self,
            "operation_semantics_version",
            _bounded_int(
                self.operation_semantics_version,
                "issue_writes.operation_semantics_version",
                default=1,
                minimum=1,
                maximum=2,
            ),
        )
        object.__setattr__(
            self,
            "max_writes_per_call",
            _bounded_int(
                self.max_writes_per_call,
                "issue_writes.max_writes_per_call",
                default=2,
                minimum=1,
                maximum=20,
            ),
        )
        object.__setattr__(
            self,
            "max_writes_per_window",
            _bounded_int(
                self.max_writes_per_window,
                "issue_writes.max_writes_per_window",
                default=20,
                minimum=1,
                maximum=10_000,
            ),
        )
        object.__setattr__(
            self,
            "window_seconds",
            _bounded_int(
                self.window_seconds,
                "issue_writes.window_seconds",
                default=3_600,
                minimum=60,
                maximum=604_800,
            ),
        )
        object.__setattr__(
            self,
            "create_title_prefix",
            _bounded_text(
                self.create_title_prefix,
                "issue_writes.create_title_prefix",
                limit=80,
            ),
        )
        object.__setattr__(
            self,
            "create_body_template",
            _template(self.create_body_template, "issue_writes.create_body_template"),
        )
        if self.max_writes_per_call > self.max_writes_per_window:
            raise IssueWritePolicyError(
                "issue_writes.max_writes_per_call cannot exceed max_writes_per_window"
            )

    @classmethod
    def from_table(cls, raw: object, *, context: str) -> IssueWritePolicy:
        if raw is None:
            return cls()
        if not isinstance(raw, dict):
            raise IssueWritePolicyError(f"{context} must be a table")
        allowed = {
            "enabled_ops",
            "approval_required_ops",
            "operation_semantics_version",
            "max_writes_per_call",
            "max_writes_per_window",
            "window_seconds",
            "create_title_prefix",
            "create_body_template",
        }
        unknown = sorted(set(raw) - allowed)
        if unknown:
            raise IssueWritePolicyError(f"{context} contains unsupported keys: {unknown}")
        try:
            return cls(
                enabled_ops=_operations(
                    raw.get("enabled_ops"),
                    f"{context}.enabled_ops",
                    default=("comment",),
                ),
                approval_required_ops=_operations(
                    raw.get("approval_required_ops"),
                    f"{context}.approval_required_ops",
                    default=(),
                ),
                operation_semantics_version=_bounded_int(
                    raw.get("operation_semantics_version"),
                    f"{context}.operation_semantics_version",
                    default=1,
                    minimum=1,
                    maximum=2,
                ),
                max_writes_per_call=_bounded_int(
                    raw.get("max_writes_per_call"),
                    f"{context}.max_writes_per_call",
                    default=2,
                    minimum=1,
                    maximum=20,
                ),
                max_writes_per_window=_bounded_int(
                    raw.get("max_writes_per_window"),
                    f"{context}.max_writes_per_window",
                    default=20,
                    minimum=1,
                    maximum=10_000,
                ),
                window_seconds=_bounded_int(
                    raw.get("window_seconds"),
                    f"{context}.window_seconds",
                    default=3_600,
                    minimum=60,
                    maximum=604_800,
                ),
                create_title_prefix=_bounded_text(
                    raw.get("create_title_prefix", "[TASK]"),
                    f"{context}.create_title_prefix",
                    limit=80,
                ),
                create_body_template=_template(
                    raw.get("create_body_template"),
                    f"{context}.create_body_template",
                ),
            )
        except IssueWritePolicyError as exc:
            message = str(exc)
            if message.startswith("issue_writes."):
                message = context + message.removeprefix("issue_writes")
            raise IssueWritePolicyError(message) from exc

    def as_table(self) -> dict[str, Any]:
        return {
            "enabled_ops": list(self.enabled_ops),
            "approval_required_ops": list(self.approval_required_ops),
            "operation_semantics_version": self.operation_semantics_version,
            "max_writes_per_call": self.max_writes_per_call,
            "max_writes_per_window": self.max_writes_per_window,
            "window_seconds": self.window_seconds,
            "create_title_prefix": self.create_title_prefix,
            "create_body_template": self.create_body_template,
        }

    def allows(self, operation: str) -> bool:
        return operation in self.enabled_ops

    def requires_approval(self, operation: str) -> bool:
        return operation in self.approval_required_ops

    def _effect_authority(self, operation: str) -> str:
        if self.operation_semantics_version == 1 and operation == IssueWriteOperation.UPDATE.value:
            return IssueWriteOperation.CREATE.value
        return operation

    def allows_effect(self, operation: str) -> bool:
        return self.allows(self._effect_authority(operation))

    def requires_effect_approval(self, operation: str) -> bool:
        return self.requires_approval(self._effect_authority(operation))

    def render_create_body(self, *, body: str, evidence_ref: str) -> str:
        return self.create_body_template.format(body=body, evidence_ref=evidence_ref)
