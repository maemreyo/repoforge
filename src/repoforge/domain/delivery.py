"""Shared guidance-delivery vocabulary (#203): how a rule or skill reaches an agent.

Delivery is orthogonal to enforcement/selection -- it only decides how often and how much of a
record's guidance rides along a tool response. Shared by the rule engine (#204) and skill
bindings (#205) so both speak the same four classes.
"""

from __future__ import annotations

from enum import Enum

MAX_ALWAYS_RECORDS = 5


class DeliveryClass(str, Enum):
    ALWAYS = "always"
    ON_ENTRY = "on_entry"
    REFRESH = "refresh"
    ON_DEMAND = "on_demand"


class DeliveryCapExceededError(ValueError):
    """Raised when more than MAX_ALWAYS_RECORDS records request `always` delivery."""

    def __init__(self, offending_id: str, *, limit: int = MAX_ALWAYS_RECORDS) -> None:
        self.offending_id = offending_id
        self.limit = limit
        super().__init__(
            f"{offending_id!r} would exceed the {limit}-record cap on 'always' delivery"
        )


def validate_always_cap(ids_by_delivery: dict[str, DeliveryClass]) -> None:
    """Reject a record set carrying more than MAX_ALWAYS_RECORDS `always`-delivery entries.

    `ids_by_delivery` must be insertion-ordered (a plain dict, as in Python 3.7+) so the
    rejection deterministically names the entry that pushed the set over the cap.
    """

    seen = 0
    for record_id, delivery in ids_by_delivery.items():
        if delivery is DeliveryClass.ALWAYS:
            seen += 1
            if seen > MAX_ALWAYS_RECORDS:
                raise DeliveryCapExceededError(record_id)
