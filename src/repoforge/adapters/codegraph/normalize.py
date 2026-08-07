"""Provider-neutral normalization for pinned CodeGraph 1.5.0 JSON output."""

from __future__ import annotations

import math
from pathlib import Path

from ...domain.code_intelligence import (
    AffectedPathCandidate,
    CodeRelationshipFact,
    CodeRelationshipKind,
)
from .normalize_contract import (
    _NODE_KINDS,
    NormalizedAffected,
    NormalizedQuery,
    NormalizedQueryNode,
    NormalizedRelationships,
    NormalizedStatus,
    _array,
    _compact_node,
    _decode,
    _integer,
    _limit,
    _object,
    _path,
    _query_node,
    _text,
)


def normalize_status(
    text: str,
    *,
    expected_version: str,
    projection_root: Path,
) -> NormalizedStatus:
    raw = _object(
        _decode(text),
        frozenset(
            {
                "initialized",
                "version",
                "projectPath",
                "indexPath",
                "lastIndexed",
                "fileCount",
                "nodeCount",
                "edgeCount",
                "dbSizeBytes",
                "backend",
                "journalMode",
                "nodesByKind",
                "languages",
                "pendingChanges",
                "worktreeMismatch",
                "index",
            }
        ),
    )
    projection = projection_root.expanduser().resolve()
    if raw["initialized"] is not True or _text(raw["version"], "version") != expected_version:
        raise ValueError("CodeGraph status does not identify the reviewed initialized provider")
    project_path = Path(_text(raw["projectPath"], "project path"))
    if not project_path.is_absolute() or project_path != projection:
        raise ValueError("CodeGraph status project path does not match the managed projection")
    index_path = Path(_text(raw["indexPath"], "index path"))
    if not index_path.is_absolute() or index_path != projection / ".index":
        raise ValueError("CodeGraph status index path does not match managed state")
    _text(raw["lastIndexed"], "last indexed timestamp")
    file_count = _integer(raw["fileCount"], "file count")
    node_count = _integer(raw["nodeCount"], "node count")
    edge_count = _integer(raw["edgeCount"], "edge count")
    _integer(raw["dbSizeBytes"], "database size")
    _text(raw["backend"], "database backend")
    _text(raw["journalMode"], "journal mode")
    kinds_raw = raw["nodesByKind"]
    if not isinstance(kinds_raw, dict):
        raise ValueError("CodeGraph nodesByKind must be an object")
    if any(kind not in _NODE_KINDS for kind in kinds_raw):
        raise ValueError("CodeGraph status contains an unknown node kind")
    for count in kinds_raw.values():
        _integer(count, "node-kind count")
    for language in _array(raw["languages"], "languages"):
        _text(language, "language")
    pending = _object(
        raw["pendingChanges"],
        frozenset({"added", "modified", "removed"}),
    )
    if any(_integer(pending[name], f"pending {name}") != 0 for name in pending):
        raise ValueError("CodeGraph index is not usable because changes remain pending")
    if raw["worktreeMismatch"] is not None:
        raise ValueError("CodeGraph index is not usable for the managed projection")
    index = _object(
        raw["index"],
        frozenset(
            {
                "builtWithVersion",
                "builtWithExtractionVersion",
                "currentExtractionVersion",
                "reindexRecommended",
                "state",
                "pendingRefs",
            }
        ),
    )
    built_version = _text(index["builtWithVersion"], "index build version")
    built_extraction = _integer(index["builtWithExtractionVersion"], "built extraction version")
    current_extraction = _integer(index["currentExtractionVersion"], "current extraction version")
    usable = (
        built_version.removeprefix("v") == expected_version
        and built_extraction == current_extraction
        and index["reindexRecommended"] is False
        and index["state"] == "complete"
        and _integer(index["pendingRefs"], "pending references") == 0
    )
    if not usable:
        raise ValueError("CodeGraph index status is not usable")
    return NormalizedStatus(file_count, node_count, edge_count)


def normalize_affected(
    text: str,
    *,
    expected_changed_paths: tuple[str, ...],
    limit: int,
) -> NormalizedAffected:
    actual_limit = _limit(limit)
    raw = _object(
        _decode(text),
        frozenset({"changedFiles", "affectedTests", "totalDependentsTraversed"}),
    )
    changed = tuple(sorted({_path(item) for item in _array(raw["changedFiles"], "changed files")}))
    expected = tuple(sorted({_path(item) for item in expected_changed_paths}))
    if changed != expected:
        raise ValueError("CodeGraph affected output does not match the reviewed changed paths")
    _integer(raw["totalDependentsTraversed"], "dependent traversal count")
    paths = tuple(sorted({_path(item) for item in _array(raw["affectedTests"], "affected tests")}))
    return NormalizedAffected(
        tuple(
            AffectedPathCandidate(path, "CodeGraph affected-file traversal.", 85)
            for path in paths[:actual_limit]
        ),
        len(paths) > actual_limit,
    )


def normalize_query(
    text: str,
    *,
    expected_symbol: str,
    expected_path: str,
    allowed_paths: frozenset[str],
    limit: int,
) -> NormalizedQuery:
    actual_limit = _limit(limit)
    symbol = _text(expected_symbol, "expected symbol")
    selected_path = _path(expected_path, allowed_paths=allowed_paths)
    matching_nodes: set[NormalizedQueryNode] = set()
    for item in _array(_decode(text), "query results"):
        result = _object(
            item,
            frozenset({"node", "score"}),
            frozenset({"highlights"}),
        )
        score = result["score"]
        if (
            not isinstance(score, (int, float))
            or isinstance(score, bool)
            or not math.isfinite(score)
        ):
            raise ValueError("CodeGraph query score must be finite")
        if "highlights" in result:
            for highlight in _array(result["highlights"], "highlights"):
                _text(highlight, "highlight", allow_empty=True)
        node = _query_node(result["node"], allowed_paths)
        matches_symbol = (
            node.name == symbol
            or node.qualified_name == symbol
            or node.qualified_name.endswith((f".{symbol}", f"::{symbol}"))
        )
        if matches_symbol:
            matching_nodes.add(node)
    if any(node.path != selected_path for node in matching_nodes):
        return NormalizedQuery((), ambiguous=True)
    ordered = tuple(
        sorted(
            (node for node in matching_nodes if node.path == selected_path),
            key=lambda item: (item.path, item.qualified_name, item.name),
        )
    )
    return NormalizedQuery(ordered[:actual_limit], len(ordered) > actual_limit)


def normalize_relationships(
    text: str,
    *,
    command: str,
    relationship_kind: CodeRelationshipKind | str,
    expected_symbol: str,
    seed_path: str,
    seed_symbol: str,
    allowed_paths: frozenset[str],
    limit: int,
) -> NormalizedRelationships:
    actual_limit = _limit(limit)
    if not isinstance(relationship_kind, CodeRelationshipKind):
        raise ValueError("CodeGraph relationship kind is not supported")
    if relationship_kind is not CodeRelationshipKind.CALLS or command not in {"callers", "callees"}:
        raise ValueError("CodeGraph relationship command is not supported")
    seed = _text(seed_symbol, "seed symbol")
    seed_file = _path(seed_path, allowed_paths=allowed_paths)
    raw = _object(_decode(text), frozenset({"symbol", command}))
    if _text(raw["symbol"], "relationship query symbol") != _text(
        expected_symbol, "expected symbol"
    ):
        raise ValueError("CodeGraph relationship output does not match the reviewed symbol")
    facts: set[CodeRelationshipFact] = set()
    for item in _array(raw[command], command):
        related, related_path = _compact_node(item, allowed_paths)
        source_path, source_symbol, target_path, target_symbol = (
            (related_path, related, seed_file, seed)
            if command == "callers"
            else (seed_file, seed, related_path, related)
        )
        facts.add(
            CodeRelationshipFact(
                relationship_kind,
                source_path,
                source_symbol,
                target_path,
                target_symbol,
                1,
                90,
            )
        )
    ordered = tuple(
        sorted(
            facts,
            key=lambda fact: (
                fact.source_path,
                fact.source_symbol,
                fact.target_path or "",
                fact.target_symbol,
            ),
        )
    )
    return NormalizedRelationships(
        ordered[:actual_limit],
        len(ordered) > actual_limit,
    )


def normalize_impact(
    text: str,
    *,
    expected_symbol: str,
    allowed_paths: frozenset[str],
    limit: int,
    max_depth: int,
) -> NormalizedAffected:
    actual_limit = _limit(limit)
    if not isinstance(max_depth, int) or isinstance(max_depth, bool) or max_depth < 1:
        raise ValueError("CodeGraph impact depth bound must be positive")
    raw = _object(
        _decode(text),
        frozenset({"symbol", "depth", "nodeCount", "edgeCount", "affected"}),
    )
    if _text(raw["symbol"], "impact symbol") != _text(expected_symbol, "expected symbol"):
        raise ValueError("CodeGraph impact output does not match the reviewed symbol")
    depth = _integer(raw["depth"], "impact depth", positive=True)
    if depth > max_depth:
        raise ValueError("CodeGraph impact output exceeds the reviewed depth")
    node_count = _integer(raw["nodeCount"], "impact node count")
    _integer(raw["edgeCount"], "impact edge count")
    paths = {
        _compact_node(item, allowed_paths)[1] for item in _array(raw["affected"], "affected nodes")
    }
    if node_count != len(paths):
        raise ValueError("CodeGraph impact node count does not match normalized paths")
    ordered = tuple(sorted(paths))
    return NormalizedAffected(
        tuple(
            AffectedPathCandidate(
                path,
                "CodeGraph symbol-impact traversal.",
                80,
                depth,
            )
            for path in ordered[:actual_limit]
        ),
        len(ordered) > actual_limit,
    )


__all__ = [
    "NormalizedAffected",
    "NormalizedQuery",
    "NormalizedQueryNode",
    "NormalizedRelationships",
    "NormalizedStatus",
    "normalize_affected",
    "normalize_impact",
    "normalize_query",
    "normalize_relationships",
    "normalize_status",
]
