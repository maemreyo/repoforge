"""Typed rule schema for the engineering constitution (#204).

A rule is a repo-authored constraint compiled from `.repoforge/rules/*.yaml`. Parsing here is
pure dict-in/dataclass-out validation -- no file I/O, no validator execution. Enforcement never
gates a receipt in V1: `checked` rules produce review findings; `advisory` rules are guidance
only. `hard` is a recognized-but-unsupported value, rejected with a specific error rather than
silently accepted or treated as an unknown enum member.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

_RULE_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_MAX_PATHS = 64
_MAX_PATH_LENGTH = 500


class Enforcement(str, Enum):
    CHECKED = "checked"
    ADVISORY = "advisory"


class OverridePolicy(str, Enum):
    NEVER = "never"
    TASK = "task"
    APPROVAL = "approval"


class RuleResultState(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    UNKNOWN = "unknown"
    SKIPPED = "skipped"
    ERROR = "error"


class RuleValidationError(ValueError):
    """A `.repoforge/rules/*.yaml` entry failed schema validation."""


class UnsupportedEnforcementError(RuleValidationError):
    """`enforcement: hard` was declared; V1 rejects it rather than silently ignoring it."""

    def __init__(self, rule_id: str) -> None:
        self.rule_id = rule_id
        super().__init__(
            f"UNSUPPORTED_ENFORCEMENT: rule {rule_id!r} declares enforcement: hard, which V1 "
            "does not support. Use 'checked' (non-blocking review finding) or 'advisory'."
        )


class UnknownValidatorError(RuleValidationError):
    """A rule references a validator id that is not a registered built-in."""

    def __init__(self, rule_id: str, validator: str, known: tuple[str, ...]) -> None:
        self.rule_id = rule_id
        self.validator = validator
        super().__init__(
            f"rule {rule_id!r} references unknown validator {validator!r}; a rule can only "
            f"reference a registered built-in (never a raw command). Known validators: "
            f"{', '.join(sorted(known))}"
        )


def _rule_id(value: object) -> str:
    if not isinstance(value, str) or _RULE_ID.fullmatch(value) is None:
        raise RuleValidationError(
            "rule id must be a lowercase dotted identifier (e.g. 'application.no-adapter-imports')"
        )
    return value


def _paths(rule_id: str, value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise RuleValidationError(f"rule {rule_id!r} must declare a non-empty 'paths' list")
    if len(value) > _MAX_PATHS:
        raise RuleValidationError(f"rule {rule_id!r} 'paths' exceeds the {_MAX_PATHS}-glob bound")
    globs: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item or len(item) > _MAX_PATH_LENGTH:
            raise RuleValidationError(f"rule {rule_id!r} has an invalid path glob: {item!r}")
        globs.append(item)
    return tuple(globs)


@dataclass(frozen=True, slots=True)
class Rule:
    id: str
    enforcement: Enforcement
    validator: str
    paths: tuple[str, ...]
    override_policy: OverridePolicy = OverridePolicy.NEVER
    delivery: str = "on_entry"
    params: dict[str, Any] = field(default_factory=dict)
    source: str = "<inline>"


def parse_rule(
    raw: dict[str, Any], *, known_validators: tuple[str, ...], source: str = "<inline>"
) -> Rule:
    """Validate one rule entry. Pure: never touches the filesystem or a validator registry
    beyond the id list the caller supplies."""

    if not isinstance(raw, dict):
        raise RuleValidationError("each rule entry must be a mapping")
    rule_id = _rule_id(raw.get("id"))

    enforcement_raw = raw.get("enforcement", Enforcement.CHECKED.value)
    if enforcement_raw == "hard":
        raise UnsupportedEnforcementError(rule_id)
    try:
        enforcement = Enforcement(enforcement_raw)
    except ValueError as exc:
        raise RuleValidationError(
            f"rule {rule_id!r} has an invalid enforcement: {enforcement_raw!r}"
        ) from exc

    validator = raw.get("validator")
    if not isinstance(validator, str) or validator not in known_validators:
        raise UnknownValidatorError(rule_id, str(validator), known_validators)

    override_raw = raw.get("override_policy", OverridePolicy.NEVER.value)
    try:
        override_policy = OverridePolicy(override_raw)
    except ValueError as exc:
        raise RuleValidationError(
            f"rule {rule_id!r} has an invalid override_policy: {override_raw!r}"
        ) from exc

    delivery = raw.get("delivery", "on_entry")
    if not isinstance(delivery, str):
        raise RuleValidationError(f"rule {rule_id!r} delivery must be a string")

    paths = _paths(rule_id, raw.get("paths"))
    params = {
        key: value
        for key, value in raw.items()
        if key not in {"id", "enforcement", "override_policy", "validator", "paths", "delivery"}
    }

    return Rule(
        id=rule_id,
        enforcement=enforcement,
        validator=validator,
        paths=paths,
        override_policy=override_policy,
        delivery=delivery,
        params=params,
        source=source,
    )


@dataclass(frozen=True, slots=True)
class Finding:
    rule_id: str
    file: str
    line: int
    message: str
    state: RuleResultState
    fix_hint: str | None = None

    def as_dict(self) -> dict[str, object]:
        item: dict[str, object] = {
            "rule_id": self.rule_id,
            "file": self.file,
            "line": self.line,
            "message": self.message,
            "state": self.state.value,
        }
        if self.fix_hint:
            item["fix_hint"] = self.fix_hint
        return item


class OverrideRejectedError(ValueError):
    """A task attempted to override a rule whose override_policy forbids it outright."""

    def __init__(self, rule_id: str, policy: OverridePolicy) -> None:
        self.rule_id = rule_id
        self.policy = policy
        super().__init__(
            f"rule {rule_id!r} has override_policy={policy.value!r}: task override is rejected"
        )


def check_override_allowed(rule_id: str, policy: OverridePolicy) -> None:
    """Raise OverrideRejectedError when `policy` forbids a task-scoped override outright.
    `task` and `approval` policies are accepted here -- the caller (TaskCapsule) is
    responsible for enforcing that an `approval` override actually carries an approval
    receipt before treating it as active."""

    if policy is OverridePolicy.NEVER:
        raise OverrideRejectedError(rule_id, policy)


def sort_findings(findings: list[Finding]) -> list[Finding]:
    """Deterministic ordering: file, then line, then rule id -- independent of validator
    execution order so two runs over the same tree always report findings identically."""

    return sorted(findings, key=lambda item: (item.file, item.line, item.rule_id))
