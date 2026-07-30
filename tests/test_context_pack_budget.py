"""Two budgets, trip independently; drops follow a declared order and are always reported.

Slice of #206. The criteria come from the ticket: "Budget squeeze drops sections in declared
order into `omitted[]`; rules survive last. Token and byte limits trip independently on
crafted fixtures."
"""

from __future__ import annotations

import pytest

from repoforge.application.rules.budget import (
    DROP_ORDER,
    OmissionReason,
    PackPart,
    PackPartKind,
    estimate_tokens,
    fit_to_budgets,
)


def _part(
    kind: PackPartKind, identifier: str, *, tokens: int = 10, byte_cost: int = 10
) -> PackPart:
    return PackPart(kind=kind, identifier=identifier, token_cost=tokens, byte_cost=byte_cost)


def _one_of_each() -> tuple[PackPart, ...]:
    return (
        _part(PackPartKind.RULES, "rules"),
        _part(PackPartKind.TASK_DECISIONS, "decisions"),
        _part(PackPartKind.ADVISORY, "advisory"),
        _part(PackPartKind.SKILL_CATALOG, "catalog"),
        _part(PackPartKind.EXAMPLES, "examples"),
    )


# ------------------------------------------------------------------- the drop order


def test_the_declared_drop_order_is_the_ticket_order() -> None:
    assert [kind.value for kind in DROP_ORDER] == [
        "examples",
        "skill_catalog",
        "advisory",
        "task_decisions",
        "rules",
    ]


def test_a_pack_that_fits_keeps_everything_and_reports_nothing_omitted() -> None:
    outcome = fit_to_budgets(_one_of_each(), token_budget=1_000, byte_budget=1_000)

    assert len(outcome.kept) == 5
    assert outcome.omitted == ()
    assert outcome.complete is True
    assert outcome.token_cost == 50
    assert outcome.byte_cost == 50


@pytest.mark.parametrize(
    ("budget", "expected_survivors"),
    [
        # Each budget admits one more part, and the order they arrive in is the reverse
        # of the drop order: rules first, examples last.
        (10, ["rules"]),
        (20, ["rules", "decisions"]),
        (30, ["rules", "decisions", "advisory"]),
        (40, ["rules", "decisions", "advisory", "catalog"]),
        (50, ["rules", "decisions", "advisory", "catalog", "examples"]),
    ],
)
def test_parts_survive_in_the_reverse_of_the_drop_order(
    budget: int,
    expected_survivors: list[str],
) -> None:
    outcome = fit_to_budgets(_one_of_each(), token_budget=budget, byte_budget=1_000)

    assert sorted(part.identifier for part in outcome.kept) == sorted(expected_survivors)


def test_rules_are_the_last_thing_to_go() -> None:
    """A budget that fits exactly one part must spend it on the rules."""

    outcome = fit_to_budgets(_one_of_each(), token_budget=10, byte_budget=1_000)

    assert [part.kind for part in outcome.kept] == [PackPartKind.RULES]
    assert {item.kind for item in outcome.omitted} == {
        PackPartKind.EXAMPLES,
        PackPartKind.SKILL_CATALOG,
        PackPartKind.ADVISORY,
        PackPartKind.TASK_DECISIONS,
    }


def test_within_one_kind_later_parts_are_dropped_first() -> None:
    """Caller order inside a kind is a priority signal, so it is respected."""

    parts = (
        _part(PackPartKind.EXAMPLES, "most-useful"),
        _part(PackPartKind.EXAMPLES, "less-useful"),
        _part(PackPartKind.EXAMPLES, "least-useful"),
    )

    outcome = fit_to_budgets(parts, token_budget=20, byte_budget=1_000)

    assert [part.identifier for part in outcome.kept] == ["most-useful", "less-useful"]
    assert [item.identifier for item in outcome.omitted] == ["least-useful"]


# ------------------------------------------------------- the budgets are independent


def test_only_the_token_budget_can_be_the_binding_constraint() -> None:
    """Token-expensive, byte-cheap: the byte budget is nowhere near the limit."""

    parts = (
        _part(PackPartKind.RULES, "rules", tokens=100, byte_cost=1),
        _part(PackPartKind.EXAMPLES, "examples", tokens=100, byte_cost=1),
    )

    outcome = fit_to_budgets(parts, token_budget=100, byte_budget=1_000_000)

    assert [part.identifier for part in outcome.kept] == ["rules"]
    assert [item.reason for item in outcome.omitted] == [OmissionReason.TOKEN_BUDGET]


def test_only_the_byte_budget_can_be_the_binding_constraint() -> None:
    """Byte-expensive, token-cheap: the mirror image, and it must behave the same way."""

    parts = (
        _part(PackPartKind.RULES, "rules", tokens=1, byte_cost=100),
        _part(PackPartKind.EXAMPLES, "examples", tokens=1, byte_cost=100),
    )

    outcome = fit_to_budgets(parts, token_budget=1_000_000, byte_budget=100)

    assert [part.identifier for part in outcome.kept] == ["rules"]
    assert [item.reason for item in outcome.omitted] == [OmissionReason.BYTE_BUDGET]


def test_both_budgets_over_at_once_is_reported_as_both() -> None:
    parts = (
        _part(PackPartKind.RULES, "rules", tokens=100, byte_cost=100),
        _part(PackPartKind.EXAMPLES, "examples", tokens=100, byte_cost=100),
    )

    outcome = fit_to_budgets(parts, token_budget=100, byte_budget=100)

    assert [item.reason for item in outcome.omitted] == [OmissionReason.BOTH_BUDGETS]


# -------------------------------------------------------------- nothing drops quietly


def test_every_dropped_part_appears_in_omitted() -> None:
    parts = _one_of_each()

    outcome = fit_to_budgets(parts, token_budget=10, byte_budget=1_000)

    kept_ids = {part.identifier for part in outcome.kept}
    omitted_ids = {item.identifier for item in outcome.omitted}
    supplied_ids = {part.identifier for part in parts}

    assert kept_ids | omitted_ids == supplied_ids
    assert kept_ids & omitted_ids == set()
    assert outcome.complete is False


def test_rules_can_be_dropped_but_never_silently() -> None:
    """A budget too small even for the rules still reports what it lost."""

    outcome = fit_to_budgets(_one_of_each(), token_budget=1, byte_budget=1)

    assert outcome.kept == ()
    assert {item.kind for item in outcome.omitted} == set(DROP_ORDER)


def test_reported_costs_describe_what_was_kept() -> None:
    outcome = fit_to_budgets(_one_of_each(), token_budget=30, byte_budget=1_000)

    assert outcome.token_cost == sum(part.token_cost for part in outcome.kept)
    assert outcome.byte_cost == sum(part.byte_cost for part in outcome.kept)
    assert outcome.token_cost <= 30


# ------------------------------------------------------------------------ validation


@pytest.mark.parametrize(("token_budget", "byte_budget"), [(0, 10), (10, 0), (-1, 10)])
def test_a_non_positive_budget_is_refused(token_budget: int, byte_budget: int) -> None:
    with pytest.raises(ValueError):
        fit_to_budgets((), token_budget=token_budget, byte_budget=byte_budget)


def test_an_unusable_part_is_refused() -> None:
    with pytest.raises(ValueError):
        PackPart(kind=PackPartKind.RULES, identifier="", token_cost=1, byte_cost=1)
    with pytest.raises(ValueError):
        PackPart(kind=PackPartKind.RULES, identifier="x", token_cost=-1, byte_cost=1)


def test_an_empty_pack_fits_any_budget() -> None:
    outcome = fit_to_budgets((), token_budget=1, byte_budget=1)

    assert outcome.kept == ()
    assert outcome.omitted == ()
    assert outcome.complete is True


# ------------------------------------------------------------- the token estimate


def test_the_token_estimate_is_monotonic_and_never_zero_for_content() -> None:
    assert estimate_tokens("") == 0
    assert estimate_tokens("a") == 1
    assert estimate_tokens("a" * 4) == 1
    assert estimate_tokens("a" * 5) == 2
    assert estimate_tokens("a" * 400) > estimate_tokens("a" * 40)


def test_the_token_estimate_counts_bytes_not_characters() -> None:
    """A multibyte string costs more than its character count suggests."""

    assert estimate_tokens("é" * 4) == estimate_tokens("a" * 8)
