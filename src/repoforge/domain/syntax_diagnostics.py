"""Bounded, source-safe syntax diagnostic evidence."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

MAX_SYNTAX_DIAGNOSTICS = 100
MAX_SYNTAX_MESSAGE_LENGTH = 500


class SyntaxDiagnosticState(str, Enum):
    OK = "ok"
    ERROR = "error"
    UNKNOWN = "unknown"


class SyntaxSeverity(str, Enum):
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class SyntaxDiagnostic:
    path: str
    line: int
    message: str
    severity: SyntaxSeverity = SyntaxSeverity.ERROR

    def __post_init__(self) -> None:
        if not self.path or len(self.path) > 4096:
            raise ValueError("syntax diagnostic path is invalid")
        if self.line < 1:
            raise ValueError("syntax diagnostic line must be positive")
        if not self.message or len(self.message) > MAX_SYNTAX_MESSAGE_LENGTH:
            raise ValueError("syntax diagnostic message is invalid")


@dataclass(frozen=True, slots=True)
class SyntaxDiagnostics:
    state: SyntaxDiagnosticState
    parse_ok: bool | None
    diagnostics: tuple[SyntaxDiagnostic, ...] = ()
    analyzed_paths: tuple[str, ...] = ()
    unknown_paths: tuple[str, ...] = ()
    truncated: bool = False

    def __post_init__(self) -> None:
        if len(self.diagnostics) > MAX_SYNTAX_DIAGNOSTICS:
            raise ValueError("syntax diagnostics exceed the reviewed bound")
        if tuple(sorted(set(self.analyzed_paths))) != self.analyzed_paths:
            raise ValueError("analyzed paths must be sorted and unique")
        if tuple(sorted(set(self.unknown_paths))) != self.unknown_paths:
            raise ValueError("unknown paths must be sorted and unique")
        if self.state is SyntaxDiagnosticState.OK:
            if self.parse_ok is not True or self.diagnostics or self.unknown_paths:
                raise ValueError("ok syntax evidence must be complete and error-free")
        elif self.state is SyntaxDiagnosticState.ERROR:
            if self.parse_ok is not False or not self.diagnostics:
                raise ValueError("error syntax evidence requires diagnostics")
        elif self.parse_ok is not None or self.diagnostics or not self.unknown_paths:
            raise ValueError("unknown syntax evidence requires unresolved paths only")


__all__ = [
    "MAX_SYNTAX_DIAGNOSTICS",
    "MAX_SYNTAX_MESSAGE_LENGTH",
    "SyntaxDiagnostic",
    "SyntaxDiagnosticState",
    "SyntaxDiagnostics",
    "SyntaxSeverity",
]
