"""Strict JSON and schema primitives for the CodeGraph adapter."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TypeAlias, cast

from ...domain.code_intelligence import (
    AffectedPathCandidate,
    CodeRelationshipFact,
)
from ...domain.errors import SecurityError
from ...domain.policy import normalize_relative_path

_JSON: TypeAlias = None | bool | int | float | str | list["_JSON"] | dict[str, "_JSON"]
_MAX_JSON_CHARS = 1_000_000
_MAX_DEPTH = 12
_MAX_COLLECTION = 2_000
_MAX_TEXT = 4_096
_NODE_KINDS = frozenset(
    {
        "file",
        "module",
        "class",
        "struct",
        "interface",
        "trait",
        "protocol",
        "function",
        "method",
        "property",
        "field",
        "variable",
        "constant",
        "enum",
        "enum_member",
        "type_alias",
        "namespace",
        "parameter",
        "import",
        "export",
        "route",
        "component",
    }
)


@dataclass(frozen=True, slots=True)
class NormalizedStatus:
    file_count: int
    node_count: int
    edge_count: int


@dataclass(frozen=True, slots=True)
class NormalizedAffected:
    candidates: tuple[AffectedPathCandidate, ...]
    truncated: bool = False


@dataclass(frozen=True, slots=True)
class NormalizedQueryNode:
    name: str
    qualified_name: str
    path: str


@dataclass(frozen=True, slots=True)
class NormalizedQuery:
    nodes: tuple[NormalizedQueryNode, ...]
    truncated: bool = False
    ambiguous: bool = False


@dataclass(frozen=True, slots=True)
class NormalizedRelationships:
    relationships: tuple[CodeRelationshipFact, ...]
    truncated: bool = False


def _duplicates(pairs: list[tuple[str, _JSON]]) -> dict[str, _JSON]:
    result: dict[str, _JSON] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("CodeGraph JSON contains duplicate keys")
        result[key] = value
    return result


def _constant(_: str) -> _JSON:
    raise ValueError("CodeGraph JSON contains a non-finite number")


def _walk(value: _JSON, depth: int = 0) -> None:
    if depth > _MAX_DEPTH:
        raise ValueError("CodeGraph JSON exceeds the reviewed nesting depth")
    if isinstance(value, str):
        if len(value) > _MAX_TEXT:
            raise ValueError("CodeGraph JSON text exceeds the reviewed bound")
        return
    if isinstance(value, list):
        if len(value) > _MAX_COLLECTION:
            raise ValueError("CodeGraph JSON collection exceeds the reviewed bound")
        for item in value:
            _walk(item, depth + 1)
        return
    if isinstance(value, dict):
        if len(value) > _MAX_COLLECTION:
            raise ValueError("CodeGraph JSON object exceeds the reviewed bound")
        for key, item in value.items():
            if len(key) > 128:
                raise ValueError("CodeGraph JSON key exceeds the reviewed bound")
            _walk(item, depth + 1)


def _decode(text: str) -> _JSON:
    if not isinstance(text, str) or not text.strip() or len(text) > _MAX_JSON_CHARS:
        raise ValueError("CodeGraph output is outside the reviewed JSON bound")
    decoder = json.JSONDecoder(object_pairs_hook=_duplicates, parse_constant=_constant)
    stripped = text.lstrip()
    try:
        decoded, end = decoder.raw_decode(stripped)
        value = cast(_JSON, decoded)
    except (json.JSONDecodeError, ValueError) as exc:
        raise ValueError("CodeGraph output is not valid strict JSON") from exc
    if stripped[end:].strip():
        raise ValueError("CodeGraph JSON contains trailing data")
    _walk(value)
    return value


def _object(
    value: _JSON,
    required: frozenset[str],
    optional: frozenset[str] = frozenset(),
) -> dict[str, _JSON]:
    if not isinstance(value, dict):
        raise ValueError("CodeGraph JSON schema requires an object")
    fields = set(value)
    if fields - required - optional or required - fields:
        raise ValueError("CodeGraph JSON schema does not match the pinned adapter")
    return value


def _array(value: _JSON, field_name: str) -> list[_JSON]:
    if not isinstance(value, list):
        raise ValueError(f"CodeGraph {field_name} must be a JSON list")
    return value


def _text(value: _JSON, field_name: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or len(value) > 512 or (not allow_empty and not value.strip()):
        raise ValueError(f"CodeGraph {field_name} must be bounded text")
    if value != value.strip() or any(ord(character) < 32 for character in value):
        raise ValueError(f"CodeGraph {field_name} contains unsupported text")
    return value


def _integer(value: _JSON, field_name: str, *, positive: bool = False) -> int:
    minimum = 1 if positive else 0
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ValueError(f"CodeGraph {field_name} must be an integer of at least {minimum}")
    return value


def _path(value: _JSON, *, allowed_paths: frozenset[str] | None = None) -> str:
    raw = _text(value, "path")
    if raw.startswith(("/", "./", "../")) or "\\" in raw:
        raise ValueError("CodeGraph path must be normalized and repository-relative")
    try:
        normalized = normalize_relative_path(raw)
    except SecurityError as exc:
        raise ValueError("CodeGraph path must be normalized and repository-relative") from exc
    if normalized != raw or (allowed_paths is not None and normalized not in allowed_paths):
        raise ValueError("CodeGraph path is outside the reviewed projection")
    return normalized


def _limit(value: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError("CodeGraph normalization limit must be a positive integer")
    return value


def _query_node(value: _JSON, allowed_paths: frozenset[str]) -> NormalizedQueryNode:
    raw = _object(
        value,
        frozenset(
            {
                "id",
                "kind",
                "name",
                "qualifiedName",
                "filePath",
                "language",
                "startLine",
                "endLine",
                "startColumn",
                "endColumn",
                "updatedAt",
            }
        ),
        frozenset(
            {
                "docstring",
                "signature",
                "visibility",
                "isExported",
                "isAsync",
                "isStatic",
                "isAbstract",
                "decorators",
                "typeParameters",
                "returnType",
            }
        ),
    )
    _text(raw["id"], "node id")
    kind = _text(raw["kind"], "node kind")
    if kind not in _NODE_KINDS:
        raise ValueError("CodeGraph query contains an unknown node kind")
    name = _text(raw["name"], "node name")
    qualified = _text(raw["qualifiedName"], "qualified node name")
    path = _path(raw["filePath"], allowed_paths=allowed_paths)
    _text(raw["language"], "node language")
    for field in ("startLine", "endLine"):
        _integer(raw[field], field, positive=True)
    for field in ("startColumn", "endColumn", "updatedAt"):
        _integer(raw[field], field)
    for field in ("docstring", "signature", "visibility", "returnType"):
        if field in raw:
            _text(raw[field], field, allow_empty=True)
    for field in ("isExported", "isAsync", "isStatic", "isAbstract"):
        if field in raw and not isinstance(raw[field], bool):
            raise ValueError(f"CodeGraph {field} must be a boolean")
    for field in ("decorators", "typeParameters"):
        if field in raw:
            for item in _array(raw[field], field):
                _text(item, field)
    return NormalizedQueryNode(name, qualified, path)


def _compact_node(value: _JSON, allowed_paths: frozenset[str]) -> tuple[str, str]:
    raw = _object(
        value,
        frozenset({"name", "kind", "filePath"}),
        frozenset({"startLine"}),
    )
    name = _text(raw["name"], "relationship symbol")
    if _text(raw["kind"], "relationship node kind") not in _NODE_KINDS:
        raise ValueError("CodeGraph relationship contains an unknown node kind")
    path = _path(raw["filePath"], allowed_paths=allowed_paths)
    if "startLine" in raw:
        _integer(raw["startLine"], "relationship start line", positive=True)
    return name, path
