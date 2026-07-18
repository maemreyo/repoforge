"""Pure domain coverage for the typed rule schema (#204)."""

from __future__ import annotations

import pytest

from repoforge.domain.delivery import (
    MAX_ALWAYS_RECORDS,
    DeliveryCapExceededError,
    DeliveryClass,
    validate_always_cap,
)
from repoforge.domain.rules_engine import (
    Enforcement,
    Finding,
    OverridePolicy,
    OverrideRejectedError,
    RuleResultState,
    RuleValidationError,
    UnknownValidatorError,
    UnsupportedEnforcementError,
    check_override_allowed,
    parse_rule,
    sort_findings,
)

_KNOWN = ("file_length", "import_boundary")


def _raw(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "id": "application.no-adapter-imports",
        "enforcement": "checked",
        "validator": "import_boundary",
        "paths": ["src/**/*.py"],
    }
    base.update(overrides)
    return base


def test_parse_rule_accepts_a_well_formed_entry() -> None:
    rule = parse_rule(_raw(forbid=["adapters"]), known_validators=_KNOWN)

    assert rule.id == "application.no-adapter-imports"
    assert rule.enforcement is Enforcement.CHECKED
    assert rule.override_policy is OverridePolicy.NEVER
    assert rule.paths == ("src/**/*.py",)
    assert rule.params == {"forbid": ["adapters"]}


def test_enforcement_hard_is_rejected_with_the_typed_error() -> None:
    with pytest.raises(UnsupportedEnforcementError) as excinfo:
        parse_rule(_raw(enforcement="hard"), known_validators=_KNOWN)
    assert "UNSUPPORTED_ENFORCEMENT" in str(excinfo.value)


def test_unknown_validator_is_rejected() -> None:
    with pytest.raises(UnknownValidatorError):
        parse_rule(_raw(validator="shell_exec"), known_validators=_KNOWN)


def test_override_policy_defaults_to_never() -> None:
    rule = parse_rule(_raw(), known_validators=_KNOWN)
    assert rule.override_policy is OverridePolicy.NEVER


@pytest.mark.parametrize("value", ["never", "task", "approval"])
def test_override_policy_accepts_all_three_values(value: str) -> None:
    rule = parse_rule(_raw(override_policy=value), known_validators=_KNOWN)
    assert rule.override_policy is OverridePolicy(value)


def test_missing_paths_is_rejected() -> None:
    raw = _raw()
    del raw["paths"]
    with pytest.raises(RuleValidationError):
        parse_rule(raw, known_validators=_KNOWN)


def test_check_override_allowed_rejects_never_and_accepts_task_and_approval() -> None:
    with pytest.raises(OverrideRejectedError):
        check_override_allowed("rule.x", OverridePolicy.NEVER)
    check_override_allowed("rule.x", OverridePolicy.TASK)
    check_override_allowed("rule.x", OverridePolicy.APPROVAL)


def test_sort_findings_is_deterministic_regardless_of_input_order() -> None:
    a = Finding("rule.b", "a.py", 5, "x", RuleResultState.FAIL)
    b = Finding("rule.a", "a.py", 5, "x", RuleResultState.FAIL)
    c = Finding("rule.a", "a.py", 1, "x", RuleResultState.FAIL)
    d = Finding("rule.a", "b.py", 1, "x", RuleResultState.FAIL)

    assert sort_findings([a, b, c, d]) == [c, b, a, d]
    assert sort_findings([d, c, b, a]) == [c, b, a, d]


def test_finding_as_dict_omits_fix_hint_when_absent() -> None:
    finding = Finding("rule.x", "a.py", 1, "message", RuleResultState.UNKNOWN)
    assert "fix_hint" not in finding.as_dict()
    with_hint = Finding("rule.x", "a.py", 1, "message", RuleResultState.FAIL, fix_hint="fix it")
    assert with_hint.as_dict()["fix_hint"] == "fix it"


def test_delivery_cap_rejects_the_sixth_always_record() -> None:
    entries = {f"rule.{i}": DeliveryClass.ALWAYS for i in range(MAX_ALWAYS_RECORDS)}
    validate_always_cap(entries)  # exactly five: fine

    entries["rule.overflow"] = DeliveryClass.ALWAYS
    with pytest.raises(DeliveryCapExceededError) as excinfo:
        validate_always_cap(entries)
    assert excinfo.value.offending_id == "rule.overflow"


def test_delivery_cap_ignores_non_always_entries() -> None:
    entries = {f"rule.{i}": DeliveryClass.ON_ENTRY for i in range(20)}
    validate_always_cap(entries)  # no cap on on_entry
