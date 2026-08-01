"""Parsing of one batched GraphQL issue payload into expanded issues.

Core issue identity/metadata is never accepted partially: any error on the
issue object itself, or a missing object without an attributable error, fails
the whole issue closed. Optional capabilities (sub-issues, dependencies,
comments) degrade independently.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from ...domain.tickets import GraphEvidenceCapability

_MAX_BODY_CHARS = 200_000
_MAX_COMMENTS = 20
_MAX_COMMENT_CHARS = 20_000
_METADATA_LINE = re.compile(r"(?im)^\s*(?:[-*]\s*)?(?P<name>[A-Za-z ]+)\s*:\s*(?P<value>[^\n]+)")


@dataclass(frozen=True, slots=True)
class ExpandedIssue:
    """One parsed issue fetched by a batched GraphQL expansion."""

    number: int
    title: str
    state: str
    body: str
    labels: tuple[str, ...]
    labels_truncated: bool
    labels_malformed: bool
    children: tuple[int, ...]
    children_truncated: bool
    children_malformed: bool
    children_external: tuple[str, ...]
    blockers: tuple[int, ...]
    blockers_truncated: bool
    blockers_malformed: bool
    blockers_external: tuple[str, ...]
    comments: tuple[str, ...]
    comments_truncated: bool
    comments_malformed: bool


def failed_capabilities(errors: list[dict[str, Any]], alias: str) -> set[GraphEvidenceCapability]:
    capabilities: set[GraphEvidenceCapability] = set()
    for error in errors:
        path = error.get("path")
        if not isinstance(path, list) or not path or path[0] != alias:
            continue
        segments = [str(item) for item in path]
        if "subIssues" in segments:
            capabilities.add(GraphEvidenceCapability.SUB_ISSUES)
        elif "blockedBy" in segments:
            capabilities.add(GraphEvidenceCapability.DEPENDENCIES)
        elif "comments" in segments:
            capabilities.add(GraphEvidenceCapability.COMMENTS)
        else:
            capabilities.add(GraphEvidenceCapability.ISSUE)
    return capabilities


def alias_issue(payload: dict[str, Any], alias: str) -> dict[str, Any] | None:
    data = payload.get("data")
    if not isinstance(data, dict):
        return None
    raw = data.get(alias)
    if not isinstance(raw, dict):
        return None
    issue = raw.get("issue")
    return issue if isinstance(issue, dict) else None


def parse_labels(raw: object) -> tuple[str, ...]:
    if not isinstance(raw, list):
        return ()
    labels: list[str] = []
    for value in raw:
        name = value.get("name") if isinstance(value, dict) else value
        if isinstance(name, str) and name.strip():
            labels.append(name.strip())
    return tuple(labels)


def parse_metadata(body: str, labels: tuple[str, ...]) -> dict[str, str]:
    values: dict[str, str] = {}
    for label in labels:
        if ":" in label:
            key, value = label.split(":", 1)
            values[key.strip().casefold()] = value.strip()
    for match in _METADATA_LINE.finditer(body):
        values.setdefault(match.group("name").strip().casefold(), match.group("value").strip())
    return values


def enum_value(enum_type: type[Any], raw: str | None) -> Any | None:
    if raw is None:
        return None
    normalized = raw.replace("_", " ").replace("-", " ").strip().casefold()
    for value in enum_type:
        candidate = str(value.value).replace("_", " ").replace("-", " ").casefold()
        if candidate == normalized:
            return value
    return None


def graphql_labels(raw: dict[str, Any]) -> tuple[tuple[str, ...], bool, bool]:
    """Return ``(labels, truncated, malformed)`` for one label connection.

    The query always requests ``totalCount``, so a connection without it
    is malformed rather than "empty and complete"; truncation is decided
    from ``totalCount`` when present.
    """
    connection = raw.get("labels")
    if not isinstance(connection, dict):
        return (), False, True
    nodes = connection.get("nodes")
    if not isinstance(nodes, list):
        return (), False, True
    total_count = connection.get("totalCount")
    if not isinstance(total_count, int) or isinstance(total_count, bool):
        return (), False, True
    labels = parse_labels(nodes)
    truncated = total_count > len(nodes)
    return labels, truncated, False


def edge_numbers(
    raw: dict[str, Any], field: str, slug: str
) -> tuple[tuple[int, ...], bool, bool, tuple[str, ...]]:
    """Return ``(numbers, truncated, malformed, external_refs)`` for one edge.

    Relationship nodes carry ``repository { nameWithOwner }`` so a
    cross-repository sub-issue or blocker is never mapped onto the same
    number in the local repository (silent identity substitution). External
    references are returned separately and the caller must degrade the
    capability instead of hydrating the wrong issue. A connection without
    ``totalCount``, or containing an invalid item, is malformed and fails
    the capability closed.
    """
    connection = raw.get(field)
    if not isinstance(connection, dict) or not isinstance(connection.get("nodes"), list):
        return (), False, True, ()
    nodes = connection["nodes"]
    total_count = connection.get("totalCount")
    page_info = connection.get("pageInfo")
    has_next_page = page_info.get("hasNextPage") if isinstance(page_info, dict) else None
    if isinstance(total_count, int) and not isinstance(total_count, bool):
        truncated = total_count > len(nodes)
    elif isinstance(has_next_page, bool):
        truncated = has_next_page
    else:
        return (), False, True, ()
    raw_numbers: list[int] = []
    external: list[str] = []
    for item in nodes:
        if not isinstance(item, dict):
            return (), False, True, ()
        number = item.get("number")
        if not isinstance(number, int) or isinstance(number, bool) or number <= 0:
            return (), False, True, ()
        repository = item.get("repository")
        name_with_owner = repository.get("nameWithOwner") if isinstance(repository, dict) else None
        if isinstance(name_with_owner, str) and name_with_owner != slug:
            external.append(f"{name_with_owner}#{number}")
            continue
        raw_numbers.append(number)
    return tuple(sorted(set(raw_numbers))), truncated, False, tuple(sorted(set(external)))


def comment_bodies(raw: dict[str, Any]) -> tuple[tuple[str, ...], bool, bool]:
    """Return ``(comment_bodies, truncated, malformed)`` for one issue."""
    connection = raw.get("comments")
    if not isinstance(connection, dict) or not isinstance(connection.get("nodes"), list):
        return (), False, True
    nodes = connection["nodes"]
    total_count = connection.get("totalCount")
    page_info = connection.get("pageInfo")
    has_next_page = page_info.get("hasNextPage") if isinstance(page_info, dict) else None
    if isinstance(total_count, int) and not isinstance(total_count, bool):
        truncated = total_count > len(nodes)
    elif isinstance(has_next_page, bool):
        truncated = has_next_page
    else:
        return (), False, True
    bodies: list[str] = []
    malformed = False
    for node in nodes[:_MAX_COMMENTS]:
        comment_body = node.get("body") if isinstance(node, dict) else None
        if not isinstance(comment_body, str) or len(comment_body) > _MAX_COMMENT_CHARS:
            malformed = True
            continue
        bodies.append(comment_body)
    return tuple(bodies), truncated, malformed


def parse_issue(raw: dict[str, Any], number: int, slug: str) -> ExpandedIssue | None:
    title = raw.get("title")
    state = raw.get("state")
    body = raw.get("body")
    if (
        raw.get("number") != number
        or not isinstance(title, str)
        or not title.strip()
        or state not in {"open", "closed", "OPEN", "CLOSED"}
        or not isinstance(body, str)
        or len(body) > _MAX_BODY_CHARS
    ):
        return None
    children, children_truncated, children_malformed, children_external = edge_numbers(
        raw, "subIssues", slug
    )
    blockers, blockers_truncated, blockers_malformed, blockers_external = edge_numbers(
        raw, "blockedBy", slug
    )
    comments, comments_truncated, comments_malformed = comment_bodies(raw)
    labels, labels_truncated, labels_malformed = graphql_labels(raw)
    return ExpandedIssue(
        number=number,
        title=title.strip(),
        state=str(state).upper(),
        body=body,
        labels=labels,
        labels_truncated=labels_truncated,
        labels_malformed=labels_malformed,
        children=children,
        children_truncated=children_truncated,
        children_malformed=children_malformed,
        children_external=children_external,
        blockers=blockers,
        blockers_truncated=blockers_truncated,
        blockers_malformed=blockers_malformed,
        blockers_external=blockers_external,
        comments=comments,
        comments_truncated=comments_truncated,
        comments_malformed=comments_malformed,
    )
