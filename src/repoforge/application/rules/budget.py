"""Fit a context pack into two independent budgets, dropping in a declared order.

A pack is bounded twice, by different things. The **transport** budget is bytes on the
wire; the **model** budget is tokens in a context window. They are not proportional -- a
byte-cheap section can be token-expensive and the reverse -- so either can be the binding
constraint, and both are checked.

What gets dropped is not "whatever came last". Order is by kind, and it is the ticket's
order: examples go first and typed rules go last, because a pack that lost its examples is
degraded while a pack that lost its rules is misleading. Every drop is reported in
``omitted`` with the budget that forced it. Nothing is dropped quietly.

Costs are supplied by the caller rather than measured here. Byte cost is exact; token cost
is not knowable without a tokenizer this repository does not depend on, so the estimate
lives in :func:`estimate_tokens` where its method is visible and replaceable, instead of
being hidden inside the fitting logic where a caller could mistake it for exact.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class PackPartKind(str, Enum):
    """What a part of the pack is, which is what decides when it is dropped."""

    RULES = "rules"
    TASK_DECISIONS = "task_decisions"
    ADVISORY = "advisory"
    SKILL_CATALOG = "skill_catalog"
    EXAMPLES = "examples"


#: Drop order, first dropped to last. The reverse of survival priority.
DROP_ORDER: tuple[PackPartKind, ...] = (
    PackPartKind.EXAMPLES,
    PackPartKind.SKILL_CATALOG,
    PackPartKind.ADVISORY,
    PackPartKind.TASK_DECISIONS,
    PackPartKind.RULES,
)


class OmissionReason(str, Enum):
    TOKEN_BUDGET = "token_budget"
    BYTE_BUDGET = "byte_budget"
    BOTH_BUDGETS = "both_budgets"


def _positive(value: int, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _non_negative(value: int, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


@dataclass(frozen=True, slots=True)
class PackPart:
    """One droppable unit of the pack, with both its costs already measured."""

    kind: PackPartKind
    identifier: str
    token_cost: int
    byte_cost: int

    def __post_init__(self) -> None:
        if not isinstance(self.kind, PackPartKind):
            raise ValueError("pack part kind must be a PackPartKind")
        if not isinstance(self.identifier, str) or not self.identifier:
            raise ValueError("pack part identifier must be a non-empty string")
        object.__setattr__(self, "token_cost", _non_negative(self.token_cost, "token_cost"))
        object.__setattr__(self, "byte_cost", _non_negative(self.byte_cost, "byte_cost"))


@dataclass(frozen=True, slots=True)
class OmittedPart:
    kind: PackPartKind
    identifier: str
    reason: OmissionReason


@dataclass(frozen=True, slots=True)
class BudgetOutcome:
    kept: tuple[PackPart, ...]
    omitted: tuple[OmittedPart, ...]
    token_cost: int
    byte_cost: int

    @property
    def complete(self) -> bool:
        """Whether the pack is everything it was asked to be."""
        return not self.omitted


def _reason(*, over_tokens: bool, over_bytes: bool) -> OmissionReason:
    if over_tokens and over_bytes:
        return OmissionReason.BOTH_BUDGETS
    return OmissionReason.TOKEN_BUDGET if over_tokens else OmissionReason.BYTE_BUDGET


def fit_to_budgets(
    parts: tuple[PackPart, ...],
    *,
    token_budget: int,
    byte_budget: int,
) -> BudgetOutcome:
    """Drop parts in :data:`DROP_ORDER` until both budgets are satisfied.

    Within one kind, later parts are dropped before earlier ones, so a caller can order
    the parts it cares most about first and have that respected.

    Rules are droppable, but only once everything else is gone. Nothing here can make a
    pack that silently lost them: a dropped rule is an entry in ``omitted``. Whether the
    compiler should instead *refuse* to answer at a budget that cannot hold the rules is a
    policy question above this function, which only decides order.
    """

    _positive(token_budget, "token_budget")
    _positive(byte_budget, "byte_budget")

    kept: list[PackPart] = list(parts)
    omitted: list[OmittedPart] = []

    def totals(items: list[PackPart]) -> tuple[int, int]:
        return (
            sum(item.token_cost for item in items),
            sum(item.byte_cost for item in items),
        )

    for kind in DROP_ORDER:
        tokens, byte_total = totals(kept)
        over_tokens = tokens > token_budget
        over_bytes = byte_total > byte_budget
        if not (over_tokens or over_bytes):
            break
        # Later parts of this kind go first; the caller's order is a priority signal.
        droppable = [index for index, item in enumerate(kept) if item.kind is kind]
        for index in reversed(droppable):
            if not (over_tokens or over_bytes):
                break
            victim = kept.pop(index)
            omitted.append(
                OmittedPart(
                    kind=victim.kind,
                    identifier=victim.identifier,
                    reason=_reason(over_tokens=over_tokens, over_bytes=over_bytes),
                )
            )
            tokens, byte_total = totals(kept)
            over_tokens = tokens > token_budget
            over_bytes = byte_total > byte_budget

    tokens, byte_total = totals(kept)
    return BudgetOutcome(
        kept=tuple(kept),
        omitted=tuple(omitted),
        token_cost=tokens,
        byte_cost=byte_total,
    )


_ESTIMATED_BYTES_PER_TOKEN = 4


def estimate_tokens(text: str) -> int:
    """Estimate a token count without depending on a tokenizer.

    Deliberately crude and deliberately visible: roughly four UTF-8 bytes per token, which
    is a reasonable average for English prose and code and wrong for anything unusual.
    Callers that need an exact count should measure with a real tokenizer and pass the
    result to :func:`fit_to_budgets` -- the fitting logic never estimates on its own, so
    swapping this out changes nothing else.
    """

    if not text:
        return 0
    encoded = len(text.encode("utf-8"))
    return max(1, -(-encoded // _ESTIMATED_BYTES_PER_TOKEN))


__all__ = [
    "DROP_ORDER",
    "BudgetOutcome",
    "OmissionReason",
    "OmittedPart",
    "PackPart",
    "PackPartKind",
    "estimate_tokens",
    "fit_to_budgets",
]
