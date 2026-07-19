"""Bounded Tree-sitter code intelligence for Python, JavaScript, and TypeScript."""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import PurePosixPath

from tree_sitter import Language, Node, Parser
from tree_sitter_javascript import language as javascript_language
from tree_sitter_python import language as python_language
from tree_sitter_typescript import language_tsx, language_typescript

from ...domain.code_intelligence import (
    MAX_CODE_INTELLIGENCE_FACTS,
    CodeImportFact,
    CodeIntelligenceMeasure,
    CodeIntelligenceRequest,
    CodeIntelligenceResult,
    CodeIntelligenceStatus,
    CodeLanguage,
    CodeReferenceFact,
    CodeSymbolFact,
    CodeSymbolKind,
    new_code_intelligence_result,
)
from .calibration import calibrated_confidence
from .syntax import (
    _LANGUAGE_BY_SUFFIX,
    _MAX_FILE_BYTES,
    _MAX_FILES,
    _MAX_TOTAL_BYTES,
    _affected_tests,
    _is_generated,
    _js_names,
    _qualified_module,
    _resolve_js_import,
    _resolve_python_import,
)

_PROVIDER_VERSION = "0.25.2+py0.25.0+js0.25.0+ts0.23.2"
_PYTHON_LANGUAGE = Language(python_language())
_JAVASCRIPT_LANGUAGE = Language(javascript_language())
_TYPESCRIPT_LANGUAGE = Language(language_typescript())
_TSX_LANGUAGE = Language(language_tsx())
_LANGUAGE_OBJECTS = {
    ".py": _PYTHON_LANGUAGE,
    ".js": _JAVASCRIPT_LANGUAGE,
    ".jsx": _JAVASCRIPT_LANGUAGE,
    ".ts": _TYPESCRIPT_LANGUAGE,
    ".tsx": _TSX_LANGUAGE,
}
_PYTHON_FROM_IMPORT = re.compile(
    r"^from\s+(?P<module>[.A-Za-z0-9_]+)\s+import\s+(?P<names>.+)$", re.S
)
_JS_FROM = re.compile(r"\bfrom\s+(['\"])(?P<target>.+?)\1", re.S)
_JS_STRING = re.compile(r"(['\"])(?P<target>.+?)\1", re.S)


def _walk(node: Node) -> Iterator[Node]:
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(reversed(current.children))


def _text(node: Node, source: bytes) -> str:
    return source[node.start_byte : node.end_byte].decode("utf-8")


def _line(node: Node) -> int:
    return node.start_point.row + 1


def _name_node(node: Node) -> Node | None:
    return node.child_by_field_name("name")


def _inside(node: Node, node_types: frozenset[str]) -> bool:
    parent = node.parent
    while parent is not None:
        if parent.type in node_types:
            return True
        parent = parent.parent
    return False


def _python_import_names(raw: str) -> tuple[str, ...]:
    normalized = raw.strip().strip("()").replace("\n", " ")
    names: set[str] = set()
    for item in normalized.split(","):
        source = item.strip().split(" as ", 1)[0].strip()
        if source and source != "*":
            names.add(source)
    return tuple(sorted(names))


def _python_facts(
    path: str,
    source: bytes,
    root: Node,
    known_paths: frozenset[str],
) -> tuple[list[CodeSymbolFact], list[CodeImportFact], list[CodeReferenceFact]]:
    symbols: list[CodeSymbolFact] = []
    imports: list[CodeImportFact] = []
    references: list[CodeReferenceFact] = []
    imported_names: dict[str, str | None] = {}
    module = _qualified_module(path)

    for node in _walk(root):
        if node.type in {"class_definition", "function_definition"}:
            name_node = _name_node(node)
            if name_node is None:
                continue
            name = _text(name_node, source)
            if node.type == "class_definition":
                kind = CodeSymbolKind.CLASS
            else:
                kind = (
                    CodeSymbolKind.METHOD
                    if _inside(node, frozenset({"class_definition"}))
                    else CodeSymbolKind.FUNCTION
                )
            symbols.append(
                CodeSymbolFact(
                    CodeLanguage.PYTHON,
                    name,
                    f"{module}.{name}" if module else name,
                    kind,
                    path,
                    _line(node),
                )
            )
        elif node.type == "import_statement":
            statement = _text(node, source).removeprefix("import").strip()
            for item in statement.split(","):
                raw = item.strip()
                if not raw:
                    continue
                target, _, alias = raw.partition(" as ")
                local_name = alias.strip() or target.strip().split(".", 1)[0]
                resolved = _resolve_python_import(path, target.strip(), 0, known_paths)
                imported_names[local_name] = resolved
                imports.append(
                    CodeImportFact(
                        CodeLanguage.PYTHON,
                        path,
                        target.strip(),
                        _line(node),
                        resolved,
                        (local_name,),
                    )
                )
        elif node.type == "import_from_statement":
            match = _PYTHON_FROM_IMPORT.match(_text(node, source).strip())
            if match is None:
                continue
            raw_module = match.group("module")
            level = len(raw_module) - len(raw_module.lstrip("."))
            module_name = raw_module.lstrip(".")
            names = _python_import_names(match.group("names"))
            resolved = _resolve_python_import(path, module_name, level, known_paths)
            for name in names:
                imported_names[name] = resolved
            imports.append(
                CodeImportFact(
                    CodeLanguage.PYTHON,
                    path,
                    raw_module,
                    _line(node),
                    resolved,
                    names,
                )
            )

    excluded = frozenset({"import_statement", "import_from_statement"})
    for node in _walk(root):
        if node.type != "identifier" or _inside(node, excluded):
            continue
        name = _text(node, source)
        if name in imported_names:
            references.append(
                CodeReferenceFact(
                    CodeLanguage.PYTHON,
                    path,
                    name,
                    _line(node),
                    imported_names[name],
                )
            )
    return symbols, imports, references


def _first_string(node: Node, source: bytes) -> str | None:
    for candidate in _walk(node):
        if candidate.type in {"string", "string_fragment"}:
            raw = _text(candidate, source).strip()
            if len(raw) >= 2 and raw[0] in {'"', "'", "`"} and raw[-1] == raw[0]:
                return raw[1:-1]
    return None


def _js_import_clause(statement: str) -> tuple[str, ...]:
    stripped = statement.strip()
    if stripped.startswith("import"):
        body = stripped[len("import") :]
        match = _JS_FROM.search(body)
        return _js_names(body[: match.start()].strip()) if match is not None else ()
    if stripped.startswith("export") and "{" in stripped and "}" in stripped:
        return _js_names(stripped[stripped.index("{") : stripped.index("}") + 1])
    return ()


def _javascript_facts(
    path: str,
    source: bytes,
    root: Node,
    language: CodeLanguage,
    known_paths: frozenset[str],
) -> tuple[list[CodeSymbolFact], list[CodeImportFact], list[CodeReferenceFact]]:
    symbols: list[CodeSymbolFact] = []
    imports: list[CodeImportFact] = []
    references: list[CodeReferenceFact] = []
    imported_names: dict[str, str | None] = {}
    module = _qualified_module(path)

    for node in _walk(root):
        kind: CodeSymbolKind | None = None
        if node.type in {"class_declaration", "abstract_class_declaration"}:
            kind = CodeSymbolKind.CLASS
        elif node.type in {
            "function_declaration",
            "generator_function_declaration",
        }:
            kind = CodeSymbolKind.FUNCTION
        elif node.type == "method_definition":
            kind = CodeSymbolKind.METHOD
        elif node.type == "variable_declarator":
            kind = CodeSymbolKind.VARIABLE
        if kind is not None:
            name_node = _name_node(node)
            if name_node is not None:
                name = _text(name_node, source)
                symbols.append(
                    CodeSymbolFact(
                        language,
                        name,
                        f"{module}.{name}" if module else name,
                        kind,
                        path,
                        _line(node),
                    )
                )

        if node.type in {"import_statement", "export_statement"}:
            statement = _text(node, source)
            target = _first_string(node, source)
            if target is None or (
                node.type == "export_statement" and " from " not in f" {statement} "
            ):
                continue
            names = _js_import_clause(statement)
            resolved = _resolve_js_import(path, target, known_paths)
            imports.append(CodeImportFact(language, path, target, _line(node), resolved, names))
            for name in names:
                imported_names[name] = resolved
        elif node.type == "call_expression":
            statement = _text(node, source).strip()
            if not (statement.startswith("import(") or statement.startswith("require(")):
                continue
            target = _first_string(node, source)
            if target is None:
                match = _JS_STRING.search(statement)
                target = match.group("target") if match is not None else None
            if target is not None:
                imports.append(
                    CodeImportFact(
                        language,
                        path,
                        target,
                        _line(node),
                        _resolve_js_import(path, target, known_paths),
                        (),
                    )
                )

    excluded = frozenset({"import_statement"})
    for node in _walk(root):
        if node.type not in {"identifier", "jsx_identifier", "type_identifier"}:
            continue
        if _inside(node, excluded):
            continue
        name = _text(node, source)
        if name in imported_names:
            references.append(
                CodeReferenceFact(language, path, name, _line(node), imported_names[name])
            )
    return symbols, imports, references


class TreeSitterCodeIntelligenceProvider:
    """Analyze supported source with pinned pre-built Tree-sitter grammars."""

    @property
    def provider_id(self) -> str:
        return "tree-sitter"

    @property
    def provider_version(self) -> str:
        return _PROVIDER_VERSION

    def analyze(self, request: CodeIntelligenceRequest) -> CodeIntelligenceResult:
        known_paths = frozenset(request.paths)
        selected = request.paths[:_MAX_FILES]
        truncated = len(request.paths) > len(selected)
        analyzed: list[str] = []
        unsupported: list[str] = []
        malformed: list[str] = []
        generated: list[str] = []
        symbols: list[CodeSymbolFact] = []
        imports: list[CodeImportFact] = []
        references: list[CodeReferenceFact] = []
        languages: set[CodeLanguage] = set()
        total_bytes = 0

        for relative_path in selected:
            if _is_generated(relative_path):
                generated.append(relative_path)
                continue
            suffix = PurePosixPath(relative_path).suffix.lower()
            language = _LANGUAGE_BY_SUFFIX.get(suffix)
            grammar = _LANGUAGE_OBJECTS.get(suffix)
            if language is None or grammar is None:
                unsupported.append(relative_path)
                continue
            candidate = request.workspace_root / relative_path
            if candidate.is_symlink() or not candidate.is_file():
                malformed.append(relative_path)
                continue
            try:
                size = candidate.stat().st_size
                source = candidate.read_bytes()
            except OSError:
                malformed.append(relative_path)
                continue
            if size > _MAX_FILE_BYTES or total_bytes + size > _MAX_TOTAL_BYTES:
                malformed.append(relative_path)
                truncated = True
                continue
            try:
                source.decode("utf-8")
            except UnicodeDecodeError:
                malformed.append(relative_path)
                continue
            total_bytes += size
            tree = Parser(grammar).parse(source)
            if tree.root_node.has_error:
                malformed.append(relative_path)
                continue
            if language is CodeLanguage.PYTHON:
                path_symbols, path_imports, path_references = _python_facts(
                    relative_path,
                    source,
                    tree.root_node,
                    known_paths,
                )
            else:
                path_symbols, path_imports, path_references = _javascript_facts(
                    relative_path,
                    source,
                    tree.root_node,
                    language,
                    known_paths,
                )
            analyzed.append(relative_path)
            languages.add(language)
            symbols.extend(path_symbols)
            imports.extend(path_imports)
            references.extend(path_references)

        for facts in (symbols, imports, references):
            if len(facts) > MAX_CODE_INTELLIGENCE_FACTS:
                del facts[MAX_CODE_INTELLIGENCE_FACTS:]
                truncated = True

        affected = _affected_tests(
            provider_id=self.provider_id,
            request=request,
            analyzed_paths=tuple(analyzed),
            imports=tuple(imports),
        )
        denominator = len(analyzed) + len(malformed) + len(request.denied_paths)
        coverage_value = round(len(analyzed) * 100 / denominator) if denominator else 0
        issues = bool(malformed or request.denied_paths or truncated)
        if not analyzed:
            status = CodeIntelligenceStatus.UNAVAILABLE
        elif issues:
            status = CodeIntelligenceStatus.PARTIAL
        else:
            status = CodeIntelligenceStatus.CURRENT
        calibrated_value, calibration_reason = calibrated_confidence(
            self.provider_id,
            frozenset(languages),
        )
        confidence_value = round(calibrated_value * coverage_value / 100)
        limitations = [
            "Tree-sitter facts cover static syntax and imports, not runtime dispatch, reflection, package-manager aliases, or generated code."
        ]
        if unsupported:
            limitations.append(
                "Unsupported file types were excluded from dependency and symbol facts."
            )
        if malformed:
            limitations.append("Malformed, unreadable, or oversized supported files were excluded.")
        if generated:
            limitations.append("Generated or vendored paths were excluded.")
        if request.denied_paths:
            limitations.append("Repository policy denied some paths before provider analysis.")
        if truncated:
            limitations.append("Provider file, byte, or fact bounds truncated the result.")
        if status is CodeIntelligenceStatus.UNAVAILABLE:
            limitations.append("No supported regular UTF-8 source file could be analyzed.")

        return new_code_intelligence_result(
            provider_id=self.provider_id,
            provider_version=self.provider_version,
            snapshot=request.snapshot,
            status=status,
            coverage=CodeIntelligenceMeasure(
                coverage_value,
                f"Analyzed {len(analyzed)} of {denominator} policy-visible paths in the bounded request.",
            ),
            confidence=CodeIntelligenceMeasure(confidence_value, calibration_reason),
            analyzed_paths=tuple(analyzed),
            symbols=tuple(symbols),
            imports=tuple(imports),
            references=tuple(references),
            affected_tests=affected,
            unsupported_paths=tuple(unsupported),
            malformed_paths=tuple(malformed),
            generated_paths=tuple(generated),
            denied_paths=request.denied_paths,
            limitations=tuple(limitations),
            truncated=truncated,
        )


__all__ = ["TreeSitterCodeIntelligenceProvider"]
