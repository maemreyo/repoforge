"""Tree-sitter syntax diagnostics over bounded virtual file content."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from pathlib import PurePosixPath

from tree_sitter import Language, Node, Parser
from tree_sitter_javascript import language as javascript_language
from tree_sitter_python import language as python_language
from tree_sitter_typescript import language_tsx, language_typescript

from ..domain.syntax_diagnostics import (
    MAX_SYNTAX_DIAGNOSTICS,
    MAX_SYNTAX_MESSAGE_LENGTH,
    SyntaxDiagnostic,
    SyntaxDiagnostics,
    SyntaxDiagnosticState,
)

_DEFAULT_FILE_BUDGET_SECONDS = 0.1
_LANGUAGE_BY_SUFFIX = {
    ".py": Language(python_language()),
    ".js": Language(javascript_language()),
    ".jsx": Language(javascript_language()),
    ".ts": Language(language_typescript()),
    ".tsx": Language(language_tsx()),
}


def _error_message(node: Node) -> str:
    if node.is_missing:
        message = f"Missing syntax element: {node.type}."
    elif node.is_error:
        message = "Unexpected syntax."
    else:
        message = "Syntax tree contains an error."
    return message[:MAX_SYNTAX_MESSAGE_LENGTH]


def _error_nodes(root: Node) -> tuple[Node, ...]:
    found: list[Node] = []
    stack = [root]
    while stack:
        node = stack.pop()
        if node.is_error or node.is_missing:
            found.append(node)
        stack.extend(reversed(node.children))
    if root.has_error and not found:
        found.append(root)
    return tuple(found)


class SyntaxDiagnosticAnalyzer:
    def __init__(
        self,
        *,
        file_budget_seconds: float = _DEFAULT_FILE_BUDGET_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if file_budget_seconds <= 0:
            raise ValueError("file_budget_seconds must be positive")
        self.file_budget_seconds = file_budget_seconds
        self.monotonic = monotonic

    def analyze(self, files: Mapping[str, bytes | None]) -> SyntaxDiagnostics:
        diagnostics: list[SyntaxDiagnostic] = []
        analyzed_paths: list[str] = []
        unknown_paths: list[str] = []
        truncated = False

        for path in sorted(files):
            source = files[path]
            if source is None:
                continue
            language = _LANGUAGE_BY_SUFFIX.get(PurePosixPath(path).suffix.lower())
            if language is None:
                unknown_paths.append(path)
                continue
            try:
                source.decode("utf-8")
            except UnicodeDecodeError:
                unknown_paths.append(path)
                continue

            started = self.monotonic()
            try:
                tree = Parser(language).parse(source)
            except Exception:
                unknown_paths.append(path)
                continue
            elapsed = self.monotonic() - started
            if elapsed > self.file_budget_seconds:
                unknown_paths.append(path)
                continue

            analyzed_paths.append(path)
            for node in _error_nodes(tree.root_node):
                if len(diagnostics) >= MAX_SYNTAX_DIAGNOSTICS:
                    truncated = True
                    continue
                diagnostics.append(
                    SyntaxDiagnostic(
                        path=path,
                        line=node.start_point.row + 1,
                        message=_error_message(node),
                    )
                )

        ordered_diagnostics = tuple(
            sorted(diagnostics, key=lambda item: (item.path, item.line, item.message))
        )
        analyzed = tuple(sorted(set(analyzed_paths)))
        unknown = tuple(sorted(set(unknown_paths)))
        if ordered_diagnostics:
            state = SyntaxDiagnosticState.ERROR
            parse_ok: bool | None = False
        elif unknown:
            state = SyntaxDiagnosticState.UNKNOWN
            parse_ok = None
        else:
            state = SyntaxDiagnosticState.OK
            parse_ok = True
        return SyntaxDiagnostics(
            state=state,
            parse_ok=parse_ok,
            diagnostics=ordered_diagnostics,
            analyzed_paths=analyzed,
            unknown_paths=unknown,
            truncated=truncated,
        )


__all__ = ["SyntaxDiagnosticAnalyzer"]
