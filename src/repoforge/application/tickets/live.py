"""Normalize bounded live GitHub issue payloads into readiness inputs."""

from __future__ import annotations

import re
from typing import Any

from ...domain.tickets import (
    PartialCompletion,
    RequirementRelation,
    RequirementRelationType,
    TicketDeliveryMetadata,
    TicketGraphError,
    TicketLiveState,
)

_MAX_LIVE_TEXT_CHARS = 200_000
_MAX_DELIVERY_ORDER = 1_000_000
_ISSUE_REFERENCE = re.compile(r"#([1-9][0-9]*)\b")
_RELATION_PATTERNS: tuple[tuple[RequirementRelationType, re.Pattern[str]], ...] = (
    (
        RequirementRelationType.SUPERSEDES,
        re.compile(r"(?im)^\s*(?:[-*]\s*)?(?:\*\*)?supersedes(?:\*\*)?\s*:\s*([^\n]+)"),
    ),
    (
        RequirementRelationType.SUPERSEDED_BY,
        re.compile(r"(?im)^\s*(?:[-*]\s*)?(?:\*\*)?superseded by(?:\*\*)?\s*:\s*([^\n]+)"),
    ),
    (
        RequirementRelationType.SPLIT_INTO,
        re.compile(r"(?im)^\s*(?:[-*]\s*)?(?:\*\*)?split into(?:\*\*)?\s*:\s*([^\n]+)"),
    ),
    (
        RequirementRelationType.MERGED_INTO,
        re.compile(r"(?im)^\s*(?:[-*]\s*)?(?:\*\*)?merged into(?:\*\*)?\s*:\s*([^\n]+)"),
    ),
    (
        RequirementRelationType.INVALIDATES,
        re.compile(r"(?im)^\s*(?:[-*]\s*)?(?:\*\*)?invalidates(?:\*\*)?\s*:\s*([^\n]+)"),
    ),
)
_PARTIAL_HEADING = re.compile(
    r"(?i)^\s*(?:[-*]\s*)?(?:\*\*)?"
    r"(verified deliverables|remaining scope|new child issues|unverified work|handoff notes)"
    r"(?:\*\*)?\s*:\s*(.*)$"
)
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


def _requirement_relations(text: str) -> tuple[RequirementRelation, ...]:
    order = {value: index for index, value in enumerate(RequirementRelationType)}
    relations: dict[tuple[RequirementRelationType, int], RequirementRelation] = {}
    for relation_type, pattern in _RELATION_PATTERNS:
        for match in pattern.finditer(text):
            for raw_target in _ISSUE_REFERENCE.findall(match.group(1)):
                target = int(raw_target)
                relations[(relation_type, target)] = RequirementRelation(
                    relation_type,
                    target,
                    f"Declared {relation_type.value} relation in live issue metadata.",
                )
    return tuple(
        sorted(
            relations.values(),
            key=lambda item: (order[item.relation_type], item.target_issue),
        )
    )


def _partial_completion(text: str) -> PartialCompletion | None:
    text_fields: dict[str, list[str]] = {
        "verified deliverables": [],
        "remaining scope": [],
        "unverified work": [],
        "handoff notes": [],
    }
    child_issues: set[int] = set()
    current: str | None = None
    found_heading = False
    for raw_line in text.splitlines():
        heading = _PARTIAL_HEADING.match(raw_line)
        if heading is not None:
            found_heading = True
            current = heading.group(1).casefold()
            inline = heading.group(2).strip().lstrip("-* ").strip()
            if inline:
                if current == "new child issues":
                    child_issues.update(int(item) for item in _ISSUE_REFERENCE.findall(inline))
                else:
                    text_fields[current].append(inline)
            continue
        stripped = raw_line.strip()
        if current is None or not stripped:
            continue
        if not stripped.startswith(("-", "*")):
            current = None
            continue
        item = stripped.lstrip("-* ").strip()
        if not item:
            continue
        if current == "new child issues":
            child_issues.update(int(value) for value in _ISSUE_REFERENCE.findall(item))
        else:
            text_fields[current].append(item)
    if not found_heading:
        return None
    return PartialCompletion(
        verified_deliverables=tuple(text_fields["verified deliverables"][:64]),
        remaining_scope=tuple(text_fields["remaining scope"][:64]),
        new_child_issues=tuple(sorted(child_issues))[:64],
        unverified_work=tuple(text_fields["unverified work"][:64]),
        handoff_notes=tuple(text_fields["handoff notes"][:64]),
    )


def _specification_complete(text: str) -> bool:
    lowered = text.casefold()
    has_objective = "objective" in lowered
    has_acceptance = "acceptance" in lowered
    has_verification = "test" in lowered or "verification" in lowered
    return has_objective and has_acceptance and has_verification


def ticket_delivery_payload(delivery: TicketDeliveryMetadata) -> dict[str, object]:
    """Render bounded requirement-evolution metadata for public read-only tools."""

    partial = delivery.partial_completion
    return {
        "relations": [
            {
                "type": item.relation_type.value,
                "target_issue": item.target_issue,
                "reason": item.reason,
            }
            for item in delivery.relations
        ],
        "partial_completion": (
            None
            if partial is None
            else {
                "verified_deliverables": list(partial.verified_deliverables),
                "remaining_scope": list(partial.remaining_scope),
                "new_child_issues": list(partial.new_child_issues),
                "unverified_work": list(partial.unverified_work),
                "handoff_notes": list(partial.handoff_notes),
                "has_remaining_scope": partial.has_remaining_scope,
            }
        ),
        "superseded_by": delivery.superseded_by,
    }


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
    relations = _requirement_relations(text)
    superseded_by = next(
        (
            item.target_issue
            for item in relations
            if item.relation_type is RequirementRelationType.SUPERSEDED_BY
        ),
        None,
    )
    return TicketLiveState(
        number=expected_number,
        is_open=is_open,
        delivery=TicketDeliveryMetadata(
            specification_complete=_specification_complete(text),
            unresolved_design_gate=_UNRESOLVED_DESIGN_GATE.search(text) is not None,
            superseded_by=superseded_by,
            relations=relations,
            partial_completion=_partial_completion(text),
            wave=_metadata_integer(_WAVE, text, "delivery wave"),
            sequence=_metadata_integer(_SEQUENCE, text, "delivery sequence"),
        ),
    )
