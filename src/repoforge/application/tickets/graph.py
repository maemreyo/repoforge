"""Offline loading, validation, and deterministic selection for roadmap tickets."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from ...domain.tickets import (
    TicketDiagnostic,
    TicketGraph,
    TicketGraphError,
    TicketLiveMetadata,
    TicketNode,
    TicketPriority,
    TicketStatus,
    TicketType,
)

_MAX_NODES = 2_000
_MAX_EDGES = 256
_MAX_ROADMAP_REFS = 32
_PRIORITY_ORDER = {
    TicketPriority.P0: 0,
    TicketPriority.P1: 1,
    TicketPriority.P2: 2,
    TicketPriority.P3: 3,
}


def _integer(value: Any, field: str, *, nullable: bool = False) -> int | None:
    if nullable and value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise TicketGraphError(f"{field} must be a positive integer")
    return value


def _string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 500:
        raise TicketGraphError(f"{field} must be a non-empty bounded string")
    return value.strip()


def _integer_tuple(value: Any, field: str) -> tuple[int, ...]:
    if not isinstance(value, list) or len(value) > _MAX_EDGES:
        raise TicketGraphError(f"{field} must be a bounded integer array")
    result: list[int] = []
    for index, item in enumerate(value):
        parsed = _integer(item, f"{field}[{index}]")
        assert parsed is not None
        result.append(parsed)
    if tuple(sorted(set(result))) != tuple(result):
        raise TicketGraphError(f"{field} must be sorted and unique")
    return tuple(result)


def _roadmap_tuple(value: Any, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or len(value) > _MAX_ROADMAP_REFS:
        raise TicketGraphError(f"{field} must be a non-empty bounded string array")
    result = tuple(_string(item, f"{field}[]") for item in value)
    if tuple(sorted(set(result))) != result:
        raise TicketGraphError(f"{field} must be sorted and unique")
    return result


def _enum(enum_type: type[Any], value: Any, field: str) -> Any:
    if not isinstance(value, str):
        raise TicketGraphError(f"{field} must be a string")
    try:
        return enum_type(value)
    except ValueError as exc:
        allowed = ", ".join(item.value for item in enum_type)
        raise TicketGraphError(f"{field} must be one of: {allowed}") from exc


def _node(value: Any, index: int) -> TicketNode:
    if not isinstance(value, dict):
        raise TicketGraphError(f"nodes[{index}] must be an object")
    required = {
        "blocks",
        "blockers",
        "children",
        "number",
        "parent",
        "priority",
        "roadmap",
        "status",
        "title",
        "type",
    }
    if set(value) != required:
        missing = sorted(required - set(value))
        extra = sorted(set(value) - required)
        raise TicketGraphError(f"nodes[{index}] fields mismatch; missing={missing}, extra={extra}")
    number = _integer(value["number"], f"nodes[{index}].number")
    parent = _integer(value["parent"], f"nodes[{index}].parent", nullable=True)
    assert number is not None
    return TicketNode(
        number=number,
        title=_string(value["title"], f"nodes[{index}].title"),
        ticket_type=_enum(TicketType, value["type"], f"nodes[{index}].type"),
        priority=_enum(TicketPriority, value["priority"], f"nodes[{index}].priority"),
        status=_enum(TicketStatus, value["status"], f"nodes[{index}].status"),
        parent=parent,
        blockers=_integer_tuple(value["blockers"], f"nodes[{index}].blockers"),
        blocks=_integer_tuple(value["blocks"], f"nodes[{index}].blocks"),
        children=_integer_tuple(value["children"], f"nodes[{index}].children"),
        roadmap=_roadmap_tuple(value["roadmap"], f"nodes[{index}].roadmap"),
    )


def load_ticket_graph(path: Path) -> TicketGraph:
    try:
        raw: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TicketGraphError(f"cannot load ticket graph: {path}") from exc
    if not isinstance(raw, dict) or set(raw) != {"nodes", "program_issue", "schema_version"}:
        raise TicketGraphError("ticket graph must contain schema_version, program_issue, and nodes")
    schema_version = _integer(raw["schema_version"], "schema_version")
    program_issue = _integer(raw["program_issue"], "program_issue")
    nodes_raw = raw["nodes"]
    if not isinstance(nodes_raw, list) or not nodes_raw or len(nodes_raw) > _MAX_NODES:
        raise TicketGraphError("nodes must be a non-empty bounded array")
    assert schema_version is not None and program_issue is not None
    if schema_version != 1:
        raise TicketGraphError(f"unsupported ticket graph schema version: {schema_version}")
    return TicketGraph(
        schema_version=schema_version,
        program_issue=program_issue,
        nodes=tuple(_node(item, index) for index, item in enumerate(nodes_raw)),
    )


def _diagnostic(code: str, number: int, message: str) -> TicketDiagnostic:
    return TicketDiagnostic(code, number, message)


def _cycle_nodes(nodes: dict[int, TicketNode]) -> set[int]:
    state: dict[int, int] = {}
    stack: list[int] = []
    result: set[int] = set()

    def visit(number: int) -> None:
        marker = state.get(number, 0)
        if marker == 2:
            return
        if marker == 1:
            try:
                start = stack.index(number)
            except ValueError:
                start = 0
            result.update(stack[start:])
            return
        state[number] = 1
        stack.append(number)
        for blocker in nodes[number].blockers:
            if blocker in nodes:
                visit(blocker)
        stack.pop()
        state[number] = 2

    for number in sorted(nodes):
        visit(number)
    return result


def validate_ticket_graph(graph: TicketGraph) -> tuple[TicketDiagnostic, ...]:
    diagnostics: list[TicketDiagnostic] = []
    counts: dict[int, int] = {}
    for node in graph.nodes:
        counts[node.number] = counts.get(node.number, 0) + 1
    for number, count in sorted(counts.items()):
        if count > 1:
            diagnostics.append(
                _diagnostic("DUPLICATE_ID", number, f"issue #{number} appears {count} times")
            )

    nodes: dict[int, TicketNode] = {}
    for node in graph.nodes:
        nodes.setdefault(node.number, node)
    program = nodes.get(graph.program_issue)
    if program is None:
        diagnostics.append(
            _diagnostic(
                "MISSING_PROGRAM",
                graph.program_issue,
                "program_issue does not reference a graph node",
            )
        )
    elif program.ticket_type is not TicketType.PROGRAM or program.parent is not None:
        diagnostics.append(
            _diagnostic(
                "INVALID_PROGRAM",
                program.number,
                "program node must have type program and no parent",
            )
        )

    derived_children: dict[int, list[int]] = {number: [] for number in nodes}
    for node in nodes.values():
        if node.parent is not None:
            if node.parent not in nodes:
                diagnostics.append(
                    _diagnostic(
                        "MISSING_PARENT",
                        node.number,
                        f"parent issue #{node.parent} is not present",
                    )
                )
            else:
                derived_children[node.parent].append(node.number)
        for blocker in node.blockers:
            target = nodes.get(blocker)
            if target is None:
                diagnostics.append(
                    _diagnostic(
                        "UNKNOWN_BLOCKER",
                        node.number,
                        f"blocker issue #{blocker} is not present",
                    )
                )
            elif node.number not in target.blocks:
                diagnostics.append(
                    _diagnostic(
                        "ASYMMETRIC_BLOCKS",
                        node.number,
                        f"blocker issue #{blocker} does not list #{node.number} in blocks",
                    )
                )
        for blocked in node.blocks:
            target = nodes.get(blocked)
            if target is None:
                diagnostics.append(
                    _diagnostic(
                        "UNKNOWN_BLOCKED_TICKET",
                        node.number,
                        f"blocked issue #{blocked} is not present",
                    )
                )
            elif node.number not in target.blockers:
                diagnostics.append(
                    _diagnostic(
                        "ASYMMETRIC_BLOCKERS",
                        node.number,
                        f"blocked issue #{blocked} does not list #{node.number} as a blocker",
                    )
                )
        if node.status is TicketStatus.READY:
            open_blockers = [
                blocker
                for blocker in node.blockers
                if blocker not in nodes or nodes[blocker].status is not TicketStatus.DONE
            ]
            if open_blockers:
                diagnostics.append(
                    _diagnostic(
                        "READY_WITH_OPEN_BLOCKER",
                        node.number,
                        f"Ready ticket has open blockers: {open_blockers}",
                    )
                )

    for number, node in sorted(nodes.items()):
        expected = tuple(sorted(derived_children[number]))
        if node.children != expected:
            diagnostics.append(
                _diagnostic(
                    "PARENT_CHILD_DRIFT",
                    number,
                    f"children={list(node.children)} but derived children={list(expected)}",
                )
            )

    for number in sorted(_cycle_nodes(nodes)):
        diagnostics.append(
            _diagnostic(
                "CIRCULAR_DEPENDENCY",
                number,
                "ticket participates in a blocker cycle",
            )
        )

    return tuple(sorted(set(diagnostics)))


def _subtree_numbers(graph: TicketGraph, root_issue: int) -> set[int]:
    nodes = {node.number: node for node in graph.nodes}
    if root_issue not in nodes:
        raise TicketGraphError(f"root_issue #{root_issue} is not present in the graph")
    result: set[int] = set()
    stack = [root_issue]
    while stack:
        current = stack.pop()
        if current in result:
            continue
        result.add(current)
        stack.extend(nodes[current].children)
    return result


def select_ready_tickets(
    graph: TicketGraph, *, limit: int, root_issue: int | None = None
) -> tuple[TicketNode, ...]:
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
        raise TicketGraphError("limit must be between 1 and 100")
    scope = _subtree_numbers(graph, root_issue) if root_issue is not None else None
    if validate_ticket_graph(graph):
        return ()
    ready = [
        node
        for node in graph.nodes
        if node.status is TicketStatus.READY
        and node.ticket_type is TicketType.IMPLEMENTATION_TICKET
        and (scope is None or node.number in scope)
    ]
    ready.sort(key=lambda node: (_PRIORITY_ORDER[node.priority], node.number))
    return tuple(ready[:limit])


_MAX_QUERY_RESULTS = 200


def select_ticket_nodes(
    graph: TicketGraph,
    *,
    root_issue: int | None = None,
    status: TicketStatus | None = None,
    priority: TicketPriority | None = None,
    initiative: int | None = None,
) -> tuple[tuple[TicketNode, ...], bool]:
    """Bounded, deterministic query over the checked-in ticket graph.

    Returns the matching nodes (sorted by issue number, capped at
    ``_MAX_QUERY_RESULTS``) and whether the result was truncated.
    """
    nodes = {node.number: node for node in graph.nodes}
    scope = _subtree_numbers(graph, root_issue) if root_issue is not None else None
    if initiative is not None:
        target = nodes.get(initiative)
        if target is None:
            raise TicketGraphError(f"initiative #{initiative} is not present in the graph")
        if target.ticket_type is not TicketType.INITIATIVE:
            raise TicketGraphError(f"issue #{initiative} is not an initiative")
        initiative_scope = _subtree_numbers(graph, initiative)
        scope = initiative_scope if scope is None else scope & initiative_scope

    matched = [
        node
        for node in graph.nodes
        if (scope is None or node.number in scope)
        and (status is None or node.status is status)
        and (priority is None or node.priority is priority)
    ]
    matched.sort(key=lambda node: node.number)
    truncated = len(matched) > _MAX_QUERY_RESULTS
    return tuple(matched[:_MAX_QUERY_RESULTS]), truncated


_NEXT_FIELD_BOUNDARY = r"(?:\.\s|\Z|\n)"


def _body_metadata(body: str, label: str) -> str | None:
    """Extract one tracked field from an issue body.

    Two conventions coexist in this project's issue history: an older
    multi-line ``**Label:** value`` form (one field per line) and the
    established terse single sentence this program's issues actually use,
    e.g. ``Parent: #101. Status: Blocked. Blocked by: #106. ...`` (with the
    last recognized field sometimes followed by free-form prose rather than
    another field, e.g. initiative bodies that omit ``Blocked by:``
    entirely). Both are matched by requiring the label to start at a field
    boundary (start of body, start of a line, or right after a ". ") and
    capturing up to the next sentence boundary rather than to end of line;
    no tracked field's own value contains a ". " or a newline.
    """
    pattern = re.compile(
        rf"(?is)(?:\A|\.\s+|\n)\s*(?:[-*]\s*)?(?:\*\*)?{re.escape(label)}(?:\*\*)?\s*:\s*"
        rf"(.+?)(?={_NEXT_FIELD_BOUNDARY})"
    )
    match = pattern.search(body)
    if match is None:
        return None
    return match.group(1).strip().rstrip(".").strip()


def compare_live_ticket_metadata(
    graph: TicketGraph,
    live_metadata: tuple[TicketLiveMetadata, ...],
) -> tuple[TicketDiagnostic, ...]:
    """Report bounded live GitHub drift without mutating either source."""
    live_by_number = {item.number: item for item in live_metadata}
    if len(live_by_number) != len(live_metadata):
        raise TicketGraphError("live metadata contains duplicate issue numbers")
    graph_by_number = {item.number: item for item in graph.nodes}
    diagnostics: list[TicketDiagnostic] = []
    for number, node in sorted(graph_by_number.items()):
        live = live_by_number.get(number)
        if live is None:
            diagnostics.append(
                _diagnostic(
                    "LIVE_ISSUE_MISSING",
                    number,
                    "live GitHub metadata did not include this graph node",
                )
            )
            continue
        if node.title != f"#{number}" and live.title != node.title:
            diagnostics.append(
                _diagnostic(
                    "LIVE_TITLE_DRIFT",
                    number,
                    f"manifest title does not match live title {live.title!r}",
                )
            )
        expected_state = "CLOSED" if node.status is TicketStatus.DONE else "OPEN"
        if live.state != expected_state:
            diagnostics.append(
                _diagnostic(
                    "LIVE_STATE_DRIFT",
                    number,
                    f"expected GitHub state {expected_state}, got {live.state}",
                )
            )
        expected_metadata = {
            "Type": node.ticket_type.value,
            "Priority": node.priority.value,
            "Status": node.status.value,
            "Parent": "None" if node.parent is None else f"#{node.parent}",
        }
        for label, expected in expected_metadata.items():
            actual = _body_metadata(live.body, label)
            # This project's established issue body is one terse sentence
            # that never restates Type/Priority (those live only in the
            # specification comment); a field absent from the body is not
            # evidence of drift, only a field present with a different
            # value is.
            if actual is not None and actual != expected:
                diagnostics.append(
                    _diagnostic(
                        "LIVE_BODY_DRIFT",
                        number,
                        f"{label} metadata expected {expected!r}, got {actual!r}",
                    )
                )
    for number in sorted(set(live_by_number) - set(graph_by_number)):
        diagnostics.append(
            _diagnostic(
                "LIVE_ISSUE_UNTRACKED",
                number,
                "live GitHub metadata contains an issue absent from the graph",
            )
        )
    return tuple(sorted(diagnostics))
