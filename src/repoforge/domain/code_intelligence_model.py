"""Shared bounded models for baseline and semantic code intelligence."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from .errors import ErrorCode, RepoForgeError

_MAX_PATHS = 2_000
_MAX_FACTS = 1_000
_MAX_LIMITATIONS = 32
_MAX_TEXT = 512
_MAX_GRAPH_DEPTH = 32
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def _invalid(message: str) -> RepoForgeError:
    return RepoForgeError(
        message,
        code=ErrorCode.CODE_INTELLIGENCE_INVALID,
        safe_next_action=(
            "Discard the result and rebuild bounded code-intelligence evidence "
            "for the exact current snapshot."
        ),
    )


def _text(value: str, field_name: str, *, limit: int = _MAX_TEXT) -> str:
    if not isinstance(value, str):
        raise _invalid(f"{field_name} must be text")
    normalized = value.strip()
    if not normalized or len(normalized) > limit:
        raise _invalid(f"{field_name} must contain between 1 and {limit} characters")
    if any(ord(character) < 32 and character not in "\n\t\r" for character in normalized):
        raise _invalid(f"{field_name} contains unsupported control characters")
    return normalized


def _safe_id(value: str, field_name: str) -> str:
    normalized = _text(value, field_name, limit=128)
    if _SAFE_ID.fullmatch(normalized) is None:
        raise _invalid(f"{field_name} has an invalid format")
    return normalized


def _path(value: str, field_name: str) -> str:
    normalized = _text(value, field_name).replace("\\", "/")
    parts = normalized.split("/")
    if normalized.startswith("/") or any(part in {"", ".", ".."} for part in parts):
        raise _invalid(f"{field_name} must be a normalized repository-relative path")
    return normalized


def _confidence(value: int, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 100:
        raise _invalid(f"{field_name} must be an integer between 0 and 100")
    return value


def _depth(value: int, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= _MAX_GRAPH_DEPTH:
        raise _invalid(f"{field_name} must be an integer between 1 and {_MAX_GRAPH_DEPTH}")
    return value


def _limitations(values: tuple[str, ...]) -> tuple[str, ...]:
    if not isinstance(values, tuple):
        raise _invalid("semantic graph limitations must be an immutable tuple")
    if len(values) > _MAX_LIMITATIONS:
        raise _invalid(f"semantic graph limitations exceed {_MAX_LIMITATIONS} items")
    return tuple(sorted({_text(value, "semantic graph limitation") for value in values}))


class CodeIntelligenceStatus(str, Enum):
    CURRENT = "current"
    PARTIAL = "partial"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class CodeIntelligenceMeasure:
    value: int
    reason: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "value", _confidence(self.value, "code-intelligence measure"))
        object.__setattr__(self, "reason", _text(self.reason, "measure reason"))


@dataclass(frozen=True, slots=True)
class AffectedTestCandidate:
    test_path: str
    reason: str
    confidence: int
    diagnostic_id: str | None = None
    selector: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "test_path", _path(self.test_path, "test_path"))
        object.__setattr__(self, "reason", _text(self.reason, "affected-test reason"))
        object.__setattr__(
            self,
            "confidence",
            _confidence(self.confidence, "affected-test confidence"),
        )
        if self.diagnostic_id is not None:
            object.__setattr__(
                self,
                "diagnostic_id",
                _safe_id(self.diagnostic_id, "diagnostic_id"),
            )
        if self.selector is not None:
            object.__setattr__(self, "selector", _text(self.selector, "diagnostic selector"))
        if (self.diagnostic_id is None) != (self.selector is None):
            raise _invalid("diagnostic_id and selector must be present together")


class CodeRelationshipKind(str, Enum):
    CALLS = "calls"
    IMPORTS = "imports"
    EXTENDS = "extends"
    IMPLEMENTS = "implements"
    REFERENCES = "references"
    INSTANTIATES = "instantiates"
    OVERRIDES = "overrides"
    ROUTES_TO = "routes_to"


@dataclass(frozen=True, slots=True)
class CodeRelationshipFact:
    kind: CodeRelationshipKind
    source_path: str
    source_symbol: str
    target_path: str | None
    target_symbol: str
    depth: int
    confidence: int

    def __post_init__(self) -> None:
        if not isinstance(self.kind, CodeRelationshipKind):
            raise _invalid("relationship kind must use CodeRelationshipKind")
        object.__setattr__(self, "source_path", _path(self.source_path, "source_path"))
        object.__setattr__(self, "source_symbol", _text(self.source_symbol, "source_symbol"))
        if self.target_path is not None:
            object.__setattr__(self, "target_path", _path(self.target_path, "target_path"))
        object.__setattr__(self, "target_symbol", _text(self.target_symbol, "target_symbol"))
        object.__setattr__(self, "depth", _depth(self.depth, "relationship depth"))
        object.__setattr__(
            self,
            "confidence",
            _confidence(self.confidence, "relationship confidence"),
        )


@dataclass(frozen=True, slots=True)
class AffectedPathCandidate:
    path: str
    reason: str
    confidence: int
    depth: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", _path(self.path, "affected path"))
        object.__setattr__(self, "reason", _text(self.reason, "affected-path reason"))
        object.__setattr__(
            self,
            "confidence",
            _confidence(self.confidence, "affected-path confidence"),
        )
        if self.depth is not None:
            object.__setattr__(self, "depth", _depth(self.depth, "affected-path depth"))


def _relationship_key(fact: CodeRelationshipFact) -> tuple[object, ...]:
    return (
        fact.kind.value,
        fact.source_path,
        fact.source_symbol,
        fact.target_path or "",
        fact.target_symbol,
        fact.depth,
        fact.confidence,
    )


def _affected_path_key(candidate: AffectedPathCandidate) -> tuple[object, ...]:
    return (
        candidate.path,
        candidate.reason,
        candidate.depth or 0,
        candidate.confidence,
    )


@dataclass(frozen=True, slots=True)
class SemanticGraphEvidence:
    provider_id: str
    provider_version: str
    status: CodeIntelligenceStatus
    coverage: CodeIntelligenceMeasure
    confidence: CodeIntelligenceMeasure
    relationships: tuple[CodeRelationshipFact, ...] = ()
    affected_paths: tuple[AffectedPathCandidate, ...] = ()
    limitations: tuple[str, ...] = ()
    truncated: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "provider_id", _safe_id(self.provider_id, "provider_id"))
        object.__setattr__(
            self,
            "provider_version",
            _text(self.provider_version, "provider_version", limit=64),
        )
        if not isinstance(self.status, CodeIntelligenceStatus):
            raise _invalid("semantic graph status must use CodeIntelligenceStatus")
        if not isinstance(self.coverage, CodeIntelligenceMeasure) or not isinstance(
            self.confidence, CodeIntelligenceMeasure
        ):
            raise _invalid("semantic graph coverage and confidence must use typed measures")
        if not isinstance(self.relationships, tuple) or any(
            not isinstance(item, CodeRelationshipFact) for item in self.relationships
        ):
            raise _invalid("semantic graph relationships must be an immutable typed tuple")
        if len(self.relationships) > _MAX_FACTS:
            raise _invalid(f"semantic graph relationships exceed {_MAX_FACTS} facts")
        if not isinstance(self.affected_paths, tuple) or any(
            not isinstance(item, AffectedPathCandidate) for item in self.affected_paths
        ):
            raise _invalid("semantic graph affected paths must be an immutable typed tuple")
        if len(self.affected_paths) > _MAX_PATHS:
            raise _invalid(f"semantic graph affected paths exceed {_MAX_PATHS} items")
        normalized_limitations = _limitations(self.limitations)
        if self.status is not CodeIntelligenceStatus.CURRENT and not normalized_limitations:
            raise _invalid("partial or unavailable semantic graph evidence requires limitations")
        if self.status is CodeIntelligenceStatus.UNAVAILABLE and (
            self.relationships or self.affected_paths
        ):
            raise _invalid("unavailable semantic graph evidence cannot contain graph facts")
        if not isinstance(self.truncated, bool):
            raise _invalid("semantic graph truncated must be a boolean")
        object.__setattr__(
            self,
            "relationships",
            tuple(sorted(set(self.relationships), key=_relationship_key)),
        )
        object.__setattr__(
            self,
            "affected_paths",
            tuple(sorted(set(self.affected_paths), key=_affected_path_key)),
        )
        object.__setattr__(self, "limitations", normalized_limitations)


__all__ = [
    "AffectedPathCandidate",
    "AffectedTestCandidate",
    "CodeIntelligenceMeasure",
    "CodeIntelligenceStatus",
    "CodeRelationshipFact",
    "CodeRelationshipKind",
    "SemanticGraphEvidence",
]
