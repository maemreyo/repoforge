"""Normalize bounded live GitHub issue payloads into readiness inputs."""

from __future__ import annotations

import re
from typing import Any

from ...domain.tickets import TicketDeliveryMetadata, TicketGraphError, TicketLiveState

_MAX_LIVE_TEXT_CHARS = 200_000
_MAX_DELIVERY_ORDER = 1_000_000
_SUPERSEDED = re.compile(r"(?im)^\s*(?:[-*]\s*)?(?:\*\*)?superseded by(?:\*\*)?\s*:\s*#(\d+)\b")
_WAVE = re.compile(r"(?im)^\s*(?:[-*]\s*)?(?:\*\*)?delivery wave(?:\*\*)?\s*:\s*(\d+)\b")
_SEQUENCE = re.compile(
    r"(?im)^\s*(?:[-*]\s*)?(?:\*\*)?(?:delivery )?sequence(?:\*\*)?\s*:\s*(\d+)\b"
)
_UNRESOLVED_DESIGN_GATE = re.compile(
    r"(?im)^\s*(?:[-*]\s*)?(?:\*\*)?(?:design gate|design decision)(?:\*\*)?\s*:\s*"
    r"(?:open|pending|unresolved|blocked)\b"
)


def _bounded_text(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return value[:_MAX_LIVE_TEXT_CHARS]


def _combined_issue_text(payload: dict[str, Any]) -> str:
    parts = [_bounded_text(payload.get("body"))]
    comments = payload.get("comments")
    if isinstance(comments, list):
        for comment in comments[:20]:
            if isinstance(comment, dict):
                parts.append(_bounded_text(comment.get("body")))
    return "\n\n".join(part for part in parts if part)[:_MAX_LIVE_TEXT_CHARS]


def _metadata_integer(pattern: re.Pattern[str], text: str, field: str) -> int:
    match = pattern.search(text)
    if match is None:
        return 0
    value = int(match.group(1))
    if value > _MAX_DELIVERY_ORDER:
        raise TicketGraphError(f"{field} exceeds {_MAX_DELIVERY_ORDER}")
    return value


def _specification_complete(text: str) -> bool:
    lowered = text.casefold()
    has_objective = "objective" in lowered
    has_acceptance = "acceptance" in lowered
    has_verification = "test" in lowered or "verification" in lowered
    return has_objective and has_acceptance and has_verification


def ticket_live_state_from_issue(payload: object, *, expected_number: int) -> TicketLiveState:
    """Normalize one bounded issue read; malformed state fails closed, never raises authority."""

    if (
        not isinstance(expected_number, int)
        or isinstance(expected_number, bool)
        or expected_number <= 0
    ):
        raise TicketGraphError("expected_number must be a positive issue number")
    if not isinstance(payload, dict) or payload.get("number") != expected_number:
        return TicketLiveState(
            expected_number,
            None,
            TicketDeliveryMetadata(specification_complete=False),
        )

    state = payload.get("state")
    is_open = True if state == "OPEN" else False if state == "CLOSED" else None
    text = _combined_issue_text(payload)
    superseded_match = _SUPERSEDED.search(text)
    superseded_by = int(superseded_match.group(1)) if superseded_match is not None else None
    return TicketLiveState(
        number=expected_number,
        is_open=is_open,
        delivery=TicketDeliveryMetadata(
            specification_complete=_specification_complete(text),
            unresolved_design_gate=_UNRESOLVED_DESIGN_GATE.search(text) is not None,
            superseded_by=superseded_by,
            wave=_metadata_integer(_WAVE, text, "delivery wave"),
            sequence=_metadata_integer(_SEQUENCE, text, "delivery sequence"),
        ),
    )
