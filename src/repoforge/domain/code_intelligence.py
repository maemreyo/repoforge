"""Provider-neutral bounded code-intelligence facts for one exact workspace snapshot."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TypeVar

from .errors import ErrorCode, RepoForgeError

MAX_CODE_INTELLIGENCE_PATHS = 2_000
MAX_CODE_INTELLIGENCE_FACTS = 1_000
MAX_CODE_INTELLIGENCE_LIMITATIONS = 32
MAX_CODE_INTELLIGENCE_TEXT = 512

_SHA40 = re.compile(r"^[a-f0-9]{40}$")
_SHA64 = re.compile(r"^[a-f0-9]{64}$")
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def _invalid(message: str) -> RepoForgeError:
    return RepoForgeError(
        message,
        code=ErrorCode.CODE_INTELLIGENCE_INVALID,
        safe_next_action="Discard the result and rebuild bounded code-intelligence evidence for the exact current snapshot.",
    )


def _text(value: str, field_name: str, *, limit: int = MAX_CODE_INTELLIGENCE_TEXT) -> str:
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


def _path(value: str, field_name: str = "path") -> str:
    normalized = _text(value, field_name).replace("\\", "/")
    parts = normalized.split("/")
    if normalized.startswith("/") or any(part in {"", ".", ".."} for part in parts):
        raise _invalid(f"{field_name} must be a normalized repository-relative path")
    return normalized


def _line(value: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise _invalid("fact line must be a positive integer")
    return value


def _bounded_unique(
    values: tuple[str, ...], field_name: str, *, paths: bool = False
) -> tuple[str, ...]:
    if not isinstance(values, tuple):
        raise _invalid(f"{field_name} must be an immutable tuple")
    if len(values) > MAX_CODE_INTELLIGENCE_PATHS:
        raise _invalid(f"{field_name} exceeds {MAX_CODE_INTELLIGENCE_PATHS} items")
    normalizer = _path if paths else lambda item, _name: _text(item, field_name)
    return tuple(sorted({normalizer(value, field_name) for value in values}))


class CodeLanguage(str, Enum):
    PYTHON = "python"
    JAVASCRIPT = "javascript"
    TYPESCRIPT = "typescript"


class CodeIntelligenceStatus(str, Enum):
    CURRENT = "current"
    PARTIAL = "partial"
    UNAVAILABLE = "unavailable"


class CodeSymbolKind(str, Enum):
    CLASS = "class"
    FUNCTION = "function"
    METHOD = "method"
    VARIABLE = "variable"


@dataclass(frozen=True, slots=True)
class CodeIntelligenceMeasure:
    value: int
    reason: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.value, int)
            or isinstance(self.value, bool)
            or not 0 <= self.value <= 100
        ):
            raise _invalid("code-intelligence measure must be an integer between 0 and 100")
        object.__setattr__(self, "reason", _text(self.reason, "measure reason"))


@dataclass(frozen=True, slots=True)
class CodeIntelligenceSnapshot:
    repo_id: str
    workspace_id: str
    head_sha: str
    workspace_fingerprint: str
    snapshot_id: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "repo_id", _safe_id(self.repo_id, "repo_id"))
        object.__setattr__(self, "workspace_id", _safe_id(self.workspace_id, "workspace_id"))
        if not isinstance(self.head_sha, str) or _SHA40.fullmatch(self.head_sha) is None:
            raise _invalid("head_sha must be a lowercase 40-character Git SHA")
        if (
            not isinstance(self.workspace_fingerprint, str)
            or _SHA64.fullmatch(self.workspace_fingerprint) is None
        ):
            raise _invalid("workspace_fingerprint must be a lowercase SHA-256")
        payload = {
            "head_sha": self.head_sha,
            "repo_id": self.repo_id,
            "workspace_fingerprint": self.workspace_fingerprint,
            "workspace_id": self.workspace_id,
        }
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        object.__setattr__(self, "snapshot_id", f"ci-{digest[:24]}")


@dataclass(frozen=True, slots=True)
class CodeSymbolFact:
    language: CodeLanguage
    name: str
    qualified_name: str
    kind: CodeSymbolKind
    path: str
    line: int

    def __post_init__(self) -> None:
        if not isinstance(self.language, CodeLanguage) or not isinstance(self.kind, CodeSymbolKind):
            raise _invalid("symbol language and kind must use typed enums")
        object.__setattr__(self, "name", _text(self.name, "symbol name"))
        object.__setattr__(self, "qualified_name", _text(self.qualified_name, "qualified name"))
        object.__setattr__(self, "path", _path(self.path, "symbol path"))
        object.__setattr__(self, "line", _line(self.line))


@dataclass(frozen=True, slots=True)
class CodeImportFact:
    language: CodeLanguage
    source_path: str
    target: str
    line: int
    resolved_path: str | None = None
    names: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.language, CodeLanguage):
            raise _invalid("import language must use CodeLanguage")
        object.__setattr__(self, "source_path", _path(self.source_path, "import source_path"))
        object.__setattr__(self, "target", _text(self.target, "import target"))
        object.__setattr__(self, "line", _line(self.line))
        if self.resolved_path is not None:
            object.__setattr__(self, "resolved_path", _path(self.resolved_path, "resolved_path"))
        object.__setattr__(
            self,
            "names",
            _bounded_unique(self.names, "import names"),
        )


@dataclass(frozen=True, slots=True)
class CodeReferenceFact:
    language: CodeLanguage
    source_path: str
    symbol: str
    line: int
    resolved_path: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.language, CodeLanguage):
            raise _invalid("reference language must use CodeLanguage")
        object.__setattr__(self, "source_path", _path(self.source_path, "reference source_path"))
        object.__setattr__(self, "symbol", _text(self.symbol, "reference symbol"))
        object.__setattr__(self, "line", _line(self.line))
        if self.resolved_path is not None:
            object.__setattr__(self, "resolved_path", _path(self.resolved_path, "resolved_path"))


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
        if (
            not isinstance(self.confidence, int)
            or isinstance(self.confidence, bool)
            or not 0 <= self.confidence <= 100
        ):
            raise _invalid("affected-test confidence must be an integer between 0 and 100")
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


@dataclass(frozen=True, slots=True)
class CodeIntelligenceRequest:
    workspace_root: Path
    snapshot: CodeIntelligenceSnapshot
    paths: tuple[str, ...]
    changed_paths: tuple[str, ...]
    diagnostic_ids: tuple[str, ...] = ()
    denied_paths: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.workspace_root, Path) or not self.workspace_root.is_absolute():
            raise _invalid("workspace_root must be an absolute Path")
        if not isinstance(self.snapshot, CodeIntelligenceSnapshot):
            raise _invalid("request snapshot must be CodeIntelligenceSnapshot")
        object.__setattr__(self, "paths", _bounded_unique(self.paths, "paths", paths=True))
        object.__setattr__(
            self,
            "changed_paths",
            _bounded_unique(self.changed_paths, "changed_paths", paths=True),
        )
        object.__setattr__(
            self,
            "diagnostic_ids",
            tuple(sorted({_safe_id(item, "diagnostic_id") for item in self.diagnostic_ids})),
        )
        object.__setattr__(
            self,
            "denied_paths",
            _bounded_unique(self.denied_paths, "denied_paths", paths=True),
        )


@dataclass(frozen=True, slots=True)
class CodeIntelligenceResult:
    provider_id: str
    provider_version: str
    snapshot: CodeIntelligenceSnapshot
    status: CodeIntelligenceStatus
    coverage: CodeIntelligenceMeasure
    confidence: CodeIntelligenceMeasure
    analyzed_paths: tuple[str, ...]
    symbols: tuple[CodeSymbolFact, ...]
    imports: tuple[CodeImportFact, ...]
    references: tuple[CodeReferenceFact, ...]
    affected_tests: tuple[AffectedTestCandidate, ...]
    unsupported_paths: tuple[str, ...]
    malformed_paths: tuple[str, ...]
    generated_paths: tuple[str, ...]
    denied_paths: tuple[str, ...]
    limitations: tuple[str, ...]
    truncated: bool = False


FactT = TypeVar("FactT")


def _facts(values: tuple[FactT, ...], field_name: str) -> tuple[FactT, ...]:
    if not isinstance(values, tuple):
        raise _invalid(f"{field_name} must be an immutable tuple")
    if len(values) > MAX_CODE_INTELLIGENCE_FACTS:
        raise _invalid(f"{field_name} exceeds {MAX_CODE_INTELLIGENCE_FACTS} facts")
    return tuple(sorted(set(values), key=repr))


def new_code_intelligence_result(
    *,
    provider_id: str,
    provider_version: str,
    snapshot: CodeIntelligenceSnapshot,
    status: CodeIntelligenceStatus,
    coverage: CodeIntelligenceMeasure,
    confidence: CodeIntelligenceMeasure,
    analyzed_paths: tuple[str, ...] = (),
    symbols: tuple[CodeSymbolFact, ...] = (),
    imports: tuple[CodeImportFact, ...] = (),
    references: tuple[CodeReferenceFact, ...] = (),
    affected_tests: tuple[AffectedTestCandidate, ...] = (),
    unsupported_paths: tuple[str, ...] = (),
    malformed_paths: tuple[str, ...] = (),
    generated_paths: tuple[str, ...] = (),
    denied_paths: tuple[str, ...] = (),
    limitations: tuple[str, ...] = (),
    truncated: bool = False,
) -> CodeIntelligenceResult:
    if not isinstance(status, CodeIntelligenceStatus):
        raise _invalid("status must be CodeIntelligenceStatus")
    if not isinstance(snapshot, CodeIntelligenceSnapshot):
        raise _invalid("snapshot must be CodeIntelligenceSnapshot")
    if not isinstance(truncated, bool):
        raise _invalid("truncated must be a boolean")
    normalized_limitations = _bounded_unique(limitations, "limitations")
    if len(normalized_limitations) > MAX_CODE_INTELLIGENCE_LIMITATIONS:
        raise _invalid(f"limitations exceeds {MAX_CODE_INTELLIGENCE_LIMITATIONS} items")
    normalized_analyzed = _bounded_unique(analyzed_paths, "analyzed_paths", paths=True)
    if status is CodeIntelligenceStatus.CURRENT and not normalized_analyzed:
        raise _invalid("current code intelligence must identify analyzed paths")
    if status is CodeIntelligenceStatus.UNAVAILABLE and normalized_analyzed:
        raise _invalid("unavailable code intelligence cannot claim analyzed paths")
    if status is not CodeIntelligenceStatus.CURRENT and not normalized_limitations:
        raise _invalid("partial or unavailable code intelligence requires explicit limitations")
    return CodeIntelligenceResult(
        provider_id=_safe_id(provider_id, "provider_id"),
        provider_version=_text(provider_version, "provider_version", limit=64),
        snapshot=snapshot,
        status=status,
        coverage=coverage,
        confidence=confidence,
        analyzed_paths=normalized_analyzed,
        symbols=_facts(symbols, "symbols"),
        imports=_facts(imports, "imports"),
        references=_facts(references, "references"),
        affected_tests=_facts(affected_tests, "affected_tests"),
        unsupported_paths=_bounded_unique(unsupported_paths, "unsupported_paths", paths=True),
        malformed_paths=_bounded_unique(malformed_paths, "malformed_paths", paths=True),
        generated_paths=_bounded_unique(generated_paths, "generated_paths", paths=True),
        denied_paths=_bounded_unique(denied_paths, "denied_paths", paths=True),
        limitations=normalized_limitations,
        truncated=truncated,
    )


def unavailable_code_intelligence(
    *,
    snapshot: CodeIntelligenceSnapshot,
    reason: str,
    provider_id: str = "unavailable",
    provider_version: str = "0",
) -> CodeIntelligenceResult:
    normalized = _text(reason, "unavailable reason")
    return new_code_intelligence_result(
        provider_id=provider_id,
        provider_version=provider_version,
        snapshot=snapshot,
        status=CodeIntelligenceStatus.UNAVAILABLE,
        coverage=CodeIntelligenceMeasure(0, normalized),
        confidence=CodeIntelligenceMeasure(0, normalized),
        limitations=(normalized,),
    )


__all__ = [
    "MAX_CODE_INTELLIGENCE_FACTS",
    "MAX_CODE_INTELLIGENCE_LIMITATIONS",
    "MAX_CODE_INTELLIGENCE_PATHS",
    "AffectedTestCandidate",
    "CodeImportFact",
    "CodeIntelligenceMeasure",
    "CodeIntelligenceRequest",
    "CodeIntelligenceResult",
    "CodeIntelligenceSnapshot",
    "CodeIntelligenceStatus",
    "CodeLanguage",
    "CodeReferenceFact",
    "CodeSymbolFact",
    "CodeSymbolKind",
    "new_code_intelligence_result",
    "unavailable_code_intelligence",
]
