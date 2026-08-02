"""GraphQL query construction for bounded batched ticket-graph reads."""

from __future__ import annotations

import json

from ...domain.tickets import GraphEvidenceCapability

_ISSUE_CORE = """
  number
  title
  state
  body
  labels(first: 50) { totalCount nodes { name } }
"""
_SUB_ISSUES_SELECTION = """
  subIssues(first: 100) {
    totalCount
    nodes { number repository { nameWithOwner } }
  }
"""
_BLOCKED_BY_SELECTION = """
  blockedBy(first: 100) {
    totalCount
    nodes { number repository { nameWithOwner } }
  }
"""
_COMMENTS_SELECTION = """
  comments(first: 20) { totalCount nodes { body } }
"""
FULL_SELECTION = " ".join(
    (_ISSUE_CORE, _SUB_ISSUES_SELECTION, _BLOCKED_BY_SELECTION, _COMMENTS_SELECTION)
)
SELECTION_FRAGMENTS: tuple[tuple[str, tuple[GraphEvidenceCapability, ...]], ...] = (
    (_ISSUE_CORE, (GraphEvidenceCapability.ISSUE,)),
    (_SUB_ISSUES_SELECTION, (GraphEvidenceCapability.SUB_ISSUES,)),
    (_BLOCKED_BY_SELECTION, (GraphEvidenceCapability.DEPENDENCIES,)),
    (_COMMENTS_SELECTION, (GraphEvidenceCapability.COMMENTS,)),
)


def build_query(slug: str, numbers: list[int], selection: str) -> str:
    owner, _, name = slug.partition("/")
    aliases = [
        (
            f"r{index}: repository(owner: {json.dumps(owner)}, "
            f"name: {json.dumps(name)}) {{ issue(number: {number}) "
            f"{{ {selection} }} }}"
        )
        for index, number in enumerate(numbers)
    ]
    return "query {{ {} }}".format(" ".join(aliases))


def selection_capabilities(selection: str) -> tuple[GraphEvidenceCapability, ...]:
    capabilities: list[GraphEvidenceCapability] = []
    for fragment, fragment_capabilities in SELECTION_FRAGMENTS:
        if fragment in selection:
            capabilities.extend(fragment_capabilities)
    return tuple(capabilities)


def stripped_selection(failed: set[GraphEvidenceCapability]) -> str:
    fragments = [
        _ISSUE_CORE,
        *([_SUB_ISSUES_SELECTION] if GraphEvidenceCapability.SUB_ISSUES not in failed else []),
        *([_BLOCKED_BY_SELECTION] if GraphEvidenceCapability.DEPENDENCIES not in failed else []),
        *([_COMMENTS_SELECTION] if GraphEvidenceCapability.COMMENTS not in failed else []),
    ]
    return " ".join(fragments)
