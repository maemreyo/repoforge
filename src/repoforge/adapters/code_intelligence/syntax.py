"""Bounded syntax/import heuristics for Python, JavaScript, and TypeScript."""

from __future__ import annotations

import ast
import posixpath
import re
from collections import deque
from pathlib import PurePosixPath

from ...domain.code_intelligence import (
    MAX_CODE_INTELLIGENCE_FACTS,
    AffectedTestCandidate,
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

_LANGUAGE_BY_SUFFIX = {
    ".py": CodeLanguage.PYTHON,
    ".js": CodeLanguage.JAVASCRIPT,
    ".jsx": CodeLanguage.JAVASCRIPT,
    ".ts": CodeLanguage.TYPESCRIPT,
    ".tsx": CodeLanguage.TYPESCRIPT,
}
_GENERATED_PARTS = frozenset(
    {
        ".venv",
        "build",
        "coverage",
        "dist",
        "generated",
        "node_modules",
        "vendor",
        "__generated__",
    }
)
_JS_IMPORT = re.compile(r"^\s*import\s+(?P<names>.+?)\s+from\s+['\"](?P<target>[^'\"]+)['\"]")
_JS_SIDE_EFFECT_IMPORT = re.compile(r"^\s*import\s+['\"](?P<target>[^'\"]+)['\"]")
_JS_REQUIRE = re.compile(r"require\(\s*['\"](?P<target>[^'\"]+)['\"]\s*\)")
_JS_FUNCTION = re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+(\w+)")
_JS_CLASS = re.compile(r"^\s*(?:export\s+)?(?:default\s+)?class\s+(\w+)")
_JS_VARIABLE = re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+(\w+)")
_MAX_FILES = 2_000
_MAX_FILE_BYTES = 512 * 1024
_MAX_TOTAL_BYTES = 12 * 1024 * 1024
_MAX_GRAPH_DEPTH = 4


def _is_generated(path: str) -> bool:
    parts = PurePosixPath(path).parts
    return any(part.lower() in _GENERATED_PARTS for part in parts)


def _is_test_path(path: str) -> bool:
    pure = PurePosixPath(path)
    name = pure.name.lower()
    return (
        "tests" in {part.lower() for part in pure.parts}
        or "__tests__" in {part.lower() for part in pure.parts}
        or name.startswith("test_")
        or name.endswith("_test.py")
        or any(marker in name for marker in (".test.", ".spec."))
    )


def _qualified_module(path: str) -> str:
    pure = PurePosixPath(path)
    stem_parts = pure.with_suffix("").parts
    if stem_parts and stem_parts[-1] == "__init__":
        stem_parts = stem_parts[:-1]
    return ".".join(stem_parts)


def _candidate_paths(base: str) -> tuple[str, ...]:
    return (
        base,
        f"{base}.py",
        f"{base}.ts",
        f"{base}.tsx",
        f"{base}.js",
        f"{base}.jsx",
        f"{base}/__init__.py",
        f"{base}/index.ts",
        f"{base}/index.tsx",
        f"{base}/index.js",
        f"{base}/index.jsx",
    )


def _resolve_python_import(
    source_path: str,
    module: str,
    level: int,
    known_paths: frozenset[str],
) -> str | None:
    source_parent = list(PurePosixPath(source_path).parent.parts)
    if level:
        remove = max(0, level - 1)
        if remove > len(source_parent):
            return None
        source_parent = source_parent[: len(source_parent) - remove]
        parts = [*source_parent, *module.split(".")] if module else source_parent
    else:
        parts = module.split(".") if module else []
    base = "/".join(part for part in parts if part)
    for candidate in _candidate_paths(base):
        if candidate in known_paths:
            return candidate
    return None


def _resolve_js_import(source_path: str, target: str, known_paths: frozenset[str]) -> str | None:
    if not target.startswith("."):
        return None
    base = posixpath.normpath(posixpath.join(str(PurePosixPath(source_path).parent), target))
    if base.startswith("../") or base == "..":
        return None
    for candidate in _candidate_paths(base):
        if candidate in known_paths:
            return candidate
    return None


def _js_names(raw: str) -> tuple[str, ...]:
    text = raw.strip()
    names: set[str] = set()
    if text.startswith("{") and "}" in text:
        body = text[1 : text.index("}")]
        for item in body.split(","):
            source = item.strip().split(" as ", 1)[0].strip()
            if source:
                names.add(source)
    elif text.startswith("*") and " as " in text:
        names.add(text.split(" as ", 1)[1].strip())
    elif text:
        names.add(text.split(",", 1)[0].strip())
    return tuple(sorted(item for item in names if item))


class _PythonCollector(ast.NodeVisitor):
    def __init__(self, path: str, known_paths: frozenset[str]) -> None:
        self.path = path
        self.known_paths = known_paths
        self.module = _qualified_module(path)
        self.scope: list[str] = []
        self.symbols: list[CodeSymbolFact] = []
        self.imports: list[CodeImportFact] = []
        self.references: list[CodeReferenceFact] = []
        self.imported_names: dict[str, str | None] = {}

    def _qualified(self, name: str) -> str:
        return ".".join(item for item in (self.module, *self.scope, name) if item)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.symbols.append(
            CodeSymbolFact(
                CodeLanguage.PYTHON,
                node.name,
                self._qualified(node.name),
                CodeSymbolKind.CLASS,
                self.path,
                node.lineno,
            )
        )
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        kind = CodeSymbolKind.METHOD if self.scope else CodeSymbolKind.FUNCTION
        self.symbols.append(
            CodeSymbolFact(
                CodeLanguage.PYTHON,
                node.name,
                self._qualified(node.name),
                kind,
                self.path,
                node.lineno,
            )
        )
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.visit_FunctionDef(node)  # type: ignore[arg-type]

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            resolved = _resolve_python_import(self.path, alias.name, 0, self.known_paths)
            local_name = alias.asname or alias.name.split(".", 1)[0]
            self.imported_names[local_name] = resolved
            self.imports.append(
                CodeImportFact(
                    CodeLanguage.PYTHON,
                    self.path,
                    alias.name,
                    node.lineno,
                    resolved,
                    (local_name,),
                )
            )

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        resolved = _resolve_python_import(self.path, module, node.level, self.known_paths)
        names = tuple(alias.asname or alias.name for alias in node.names)
        for name in names:
            self.imported_names[name] = resolved
        target = "." * node.level + module
        self.imports.append(
            CodeImportFact(
                CodeLanguage.PYTHON,
                self.path,
                target or ".",
                node.lineno,
                resolved,
                names,
            )
        )

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Load) and node.id in self.imported_names:
            self.references.append(
                CodeReferenceFact(
                    CodeLanguage.PYTHON,
                    self.path,
                    node.id,
                    node.lineno,
                    self.imported_names[node.id],
                )
            )


def _python_facts(
    path: str,
    content: str,
    known_paths: frozenset[str],
) -> tuple[list[CodeSymbolFact], list[CodeImportFact], list[CodeReferenceFact]]:
    tree = ast.parse(content, filename=path)
    collector = _PythonCollector(path, known_paths)
    collector.visit(tree)
    return collector.symbols, collector.imports, collector.references


def _javascript_facts(
    path: str,
    content: str,
    language: CodeLanguage,
    known_paths: frozenset[str],
) -> tuple[list[CodeSymbolFact], list[CodeImportFact], list[CodeReferenceFact]]:
    symbols: list[CodeSymbolFact] = []
    imports: list[CodeImportFact] = []
    references: list[CodeReferenceFact] = []
    imported_names: dict[str, str | None] = {}
    module = _qualified_module(path)
    for line_number, line in enumerate(content.splitlines(), start=1):
        matched_symbol: tuple[re.Pattern[str], CodeSymbolKind] | None = None
        for pattern, kind in (
            (_JS_FUNCTION, CodeSymbolKind.FUNCTION),
            (_JS_CLASS, CodeSymbolKind.CLASS),
            (_JS_VARIABLE, CodeSymbolKind.VARIABLE),
        ):
            if pattern.search(line):
                matched_symbol = (pattern, kind)
                break
        if matched_symbol is not None:
            match = matched_symbol[0].search(line)
            assert match is not None
            name = match.group(1)
            symbols.append(
                CodeSymbolFact(
                    language, name, f"{module}.{name}", matched_symbol[1], path, line_number
                )
            )

        import_match = _JS_IMPORT.search(line)
        side_effect = _JS_SIDE_EFFECT_IMPORT.search(line)
        require_match = _JS_REQUIRE.search(line)
        if import_match is not None:
            target = import_match.group("target")
            names = _js_names(import_match.group("names"))
        elif side_effect is not None:
            target = side_effect.group("target")
            names = ()
        elif require_match is not None:
            target = require_match.group("target")
            assignment = line.split("=", 1)[0].strip().split()[-1] if "=" in line else ""
            names = (assignment,) if assignment else ()
        else:
            continue
        resolved = _resolve_js_import(path, target, known_paths)
        imports.append(CodeImportFact(language, path, target, line_number, resolved, names))
        for name in names:
            imported_names[name] = resolved

    for line_number, line in enumerate(content.splitlines(), start=1):
        for name, resolved in imported_names.items():
            if re.search(rf"\b{re.escape(name)}\b", line):
                references.append(CodeReferenceFact(language, path, name, line_number, resolved))
    return symbols, imports, references


def _same_stem(source_path: str, test_path: str) -> bool:
    source = PurePosixPath(source_path).stem.lower().removeprefix("test_").removesuffix("_test")
    test = PurePosixPath(test_path).stem.lower().removeprefix("test_").removesuffix("_test")
    return bool(source and source == test)


def _affected_tests(
    *,
    provider_id: str,
    request: CodeIntelligenceRequest,
    analyzed_paths: tuple[str, ...],
    imports: tuple[CodeImportFact, ...],
) -> tuple[AffectedTestCandidate, ...]:
    changed = frozenset(request.changed_paths)
    tests = tuple(path for path in analyzed_paths if _is_test_path(path))
    graph: dict[str, set[str]] = {}
    for fact in imports:
        if fact.resolved_path is not None:
            graph.setdefault(fact.source_path, set()).add(fact.resolved_path)
    candidates: dict[str, AffectedTestCandidate] = {}
    for test_path in tests:
        reason: str | None = None
        if test_path in changed:
            reason = "The test file itself changed."
        else:
            queue: deque[tuple[str, int]] = deque([(test_path, 0)])
            seen = {test_path}
            while queue and reason is None:
                current, depth = queue.popleft()
                if depth >= _MAX_GRAPH_DEPTH:
                    continue
                for dependency in sorted(graph.get(current, ())):
                    if dependency in changed:
                        reason = (
                            "The test imports a changed source directly."
                            if depth == 0
                            else f"The test reaches a changed source through {depth + 1} import hops."
                        )
                        break
                    if dependency not in seen:
                        seen.add(dependency)
                        queue.append((dependency, depth + 1))
        if reason is None:
            matched = next((path for path in sorted(changed) if _same_stem(path, test_path)), None)
            if matched is not None:
                reason = f"The test name matches changed source {matched}."
        if reason is None:
            continue
        language = _LANGUAGE_BY_SUFFIX.get(PurePosixPath(test_path).suffix.lower())
        if language is None:
            continue
        confidence, _calibration_reason = calibrated_confidence(
            provider_id,
            frozenset({language}),
        )
        diagnostic_id = (
            "pytest-target"
            if language is CodeLanguage.PYTHON and "pytest-target" in request.diagnostic_ids
            else None
        )
        candidates[test_path] = AffectedTestCandidate(
            test_path=test_path,
            reason=reason,
            confidence=confidence,
            diagnostic_id=diagnostic_id,
            selector=test_path if diagnostic_id is not None else None,
        )
    return tuple(candidates[path] for path in sorted(candidates))


class SyntaxCodeIntelligenceProvider:
    """Analyze bounded local source files without daemons, network, or raw query languages."""

    @property
    def provider_id(self) -> str:
        return "syntax"

    @property
    def provider_version(self) -> str:
        return "1"

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
        total_bytes = 0

        for relative_path in selected:
            if _is_generated(relative_path):
                generated.append(relative_path)
                continue
            language = _LANGUAGE_BY_SUFFIX.get(PurePosixPath(relative_path).suffix.lower())
            if language is None:
                unsupported.append(relative_path)
                continue
            candidate = request.workspace_root / relative_path
            if candidate.is_symlink() or not candidate.is_file():
                malformed.append(relative_path)
                continue
            try:
                size = candidate.stat().st_size
            except OSError:
                malformed.append(relative_path)
                continue
            if size > _MAX_FILE_BYTES or total_bytes + size > _MAX_TOTAL_BYTES:
                malformed.append(relative_path)
                truncated = True
                continue
            try:
                content = candidate.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                malformed.append(relative_path)
                continue
            total_bytes += size
            try:
                if language is CodeLanguage.PYTHON:
                    path_symbols, path_imports, path_references = _python_facts(
                        relative_path, content, known_paths
                    )
                else:
                    path_symbols, path_imports, path_references = _javascript_facts(
                        relative_path, content, language, known_paths
                    )
            except (SyntaxError, ValueError):
                malformed.append(relative_path)
                continue
            analyzed.append(relative_path)
            symbols.extend(path_symbols)
            imports.extend(path_imports)
            references.extend(path_references)

        if len(symbols) > MAX_CODE_INTELLIGENCE_FACTS:
            symbols = symbols[:MAX_CODE_INTELLIGENCE_FACTS]
            truncated = True
        if len(imports) > MAX_CODE_INTELLIGENCE_FACTS:
            imports = imports[:MAX_CODE_INTELLIGENCE_FACTS]
            truncated = True
        if len(references) > MAX_CODE_INTELLIGENCE_FACTS:
            references = references[:MAX_CODE_INTELLIGENCE_FACTS]
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
        analyzed_languages = frozenset(
            language
            for path in analyzed
            if (language := _LANGUAGE_BY_SUFFIX.get(PurePosixPath(path).suffix.lower())) is not None
        )
        calibrated_value, calibration_reason = calibrated_confidence(
            self.provider_id,
            analyzed_languages,
        )
        confidence_value = round(calibrated_value * coverage_value / 100)
        limitations = [
            "Syntax-only analysis does not resolve runtime dispatch, reflection, aliases across package managers, or generated code."
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


__all__ = ["SyntaxCodeIntelligenceProvider"]
